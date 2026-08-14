from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Image
from astrbot.api.star import StarTools
from mcp.types import CallToolResult, ImageContent, TextContent

from .api import (
    ArkImageClient,
    ImageApiError,
    ImageConfigError,
    aspect_to_size,
    sniff_mime,
    to_data_url,
    validate_reference_image,
)
from .store import GeneratedImage, GeneratedImageStore

PLUGIN_NAME = "astrbot_plugin_yangmo_image_generation"
VERSION = "0.2.1"
MAX_REFERENCE_IMAGES = 14
MAX_REFERENCE_BYTES = 30 * 1024 * 1024
SUPPORTED_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def _scope(event: AstrMessageEvent) -> str:
    platform = str(getattr(event, "get_platform_id", lambda: "")() or "")
    self_id = str(event.get_self_id() or "")
    group_id = str(event.get_group_id() or "")
    if group_id:
        return f"{platform}:{self_id}:group:{group_id}"
    return f"{platform}:{self_id}:private:{event.get_sender_id()}"


def _normalize_refs(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").replace("，", ",").replace("、", ",")
    for separator in (",", "\n", "\t"):
        text = text.replace(separator, " ")
    return [item for item in text.split(" ") if item]


class IndependentImageGeneration(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        root = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.store = GeneratedImageStore(root)
        self.client = ArkImageClient(config)
        self._cleanup_lock = asyncio.Lock()
        self._last_cleanup = 0.0
        logger.info("[yangmo.image] ready version=%s store=%s", VERSION, root)

    @filter.llm_tool(name="generate_image")
    async def generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
        refs: list[str] | None = None,
        count: int = 1,
        aspect: str = "landscape",
        auto_send: bool = True,
    ) -> CallToolResult:
        """直接生成、编辑或合成图片；默认生成成功后立即把原文件发送到当前聊天。

        这是一个可在任意模型步骤调用的 Agent 工具，没有 prepare、句柄或强制前后流程。
        模型可以在调用前后自由穿插自然语言、其他工具和继续推理；本工具只负责图片副作用。

        契约：p ::= 非空完整图片指令；
        r ::= "current" | "genimg:" + 16 位十六进制；R ::= [] | [r1,...,rk]；1 <= n <= 15；
        generate(p,R,n,a,auto_send=true) -> G + preview + delivery。
        auto_send=true 是默认 harness 行为：生成完成即交付原文件，不需要模型再调用发送工具。
        如果当前 Agent 明确需要先观察、继续编辑、比较候选或暂不交付，可传 auto_send=false。
        无论是否自动发送，都会返回 genimg: 引用和内部 preview，供当前 AI 后续继续使用。
        工具调用会立即执行真实图片 API，请只在当前推理确实需要生成/编辑图片时调用。

        Args:
            prompt(string): p；完整画面描述或编辑指令。
            refs(list[string]): R；可选，仅接受 current 或本插件 genimg 提取码。
            count(number): n；需要的图片数量，范围 1..15（API 参数边界，不是插件配额）。
            aspect(string): a；landscape/portrait/square/photo/wide 或 W:H。
            auto_send(boolean): 是否在生成成功后自动发送原文件；默认 true。
        """
        prompt_text = str(prompt or "").strip()
        if not prompt_text:
            return _error_result("prompt 不能为空。")
        try:
            image_count = int(count)
        except (TypeError, ValueError):
            return _error_result("count 必须是整数。")
        if image_count < 1 or image_count > 15:
            return _error_result("count 必须在 1 到 15 之间。")

        try:
            reference_urls, reference_manifest = await self._resolve_references(
                event, _normalize_refs(refs)
            )
        except Exception as exc:
            return _error_result(f"参考图解析失败：{str(exc)[:300]}")
        if refs and not reference_urls:
            return _error_result("没有任何参考图可用。")

        try:
            size = aspect_to_size(aspect, self.config)
            self.client.preflight(prompt_text, size, reference_urls, image_count)
            await self._maybe_prune()
            urls, api_calls, model, used_plan = await self.client.generate(
                prompt_text, size, reference_urls, image_count
            )
            generated = []
            download_errors = []
            for url in urls:
                try:
                    data = await self.client.download(url)
                    mime = sniff_mime(data)
                    if mime not in SUPPORTED_IMAGE_MIME:
                        raise ValueError("下载结果不是受支持的图片格式")
                    generated.append(
                        await asyncio.to_thread(
                            self.store.put,
                            scope=_scope(event),
                            data=data,
                            mime_type=mime,
                        )
                    )
                except Exception as exc:
                    download_errors.append(str(exc)[:240])
        except ImageApiError as exc:
            return _error_result(str(exc), api_calls=exc.api_calls)
        except ImageConfigError as exc:
            return _error_result(str(exc), api_calls=exc.api_calls)
        except ValueError as exc:
            return _error_result(str(exc), api_calls=0)
        except Exception as exc:
            logger.error("[yangmo.image] generation failed", exc_info=True)
            return _error_result(f"生成失败：{str(exc)[:300]}")
        if not generated:
            return _error_result(
                "方舟返回了结果，但原图全部下载或落库失败：" + "; ".join(download_errors),
                api_calls=api_calls,
            )

        rows = [
            {
                "ref": image.ref,
                "mime_type": image.mime_type,
                "bytes": image.size,
                "sha256": image.sha256,
            }
            for image in generated
        ]

        delivery = {"mode": "deferred", "sent": 0, "results": []}
        if bool(auto_send):
            delivery = await self._deliver_images(event, generated)
            delivery["mode"] = "automatic"

        payload = {
            "status": "ok",
            "generated": rows,
            "model": model,
            "api_calls": api_calls,
            "requested_count": image_count,
            "returned_count": len(rows),
            "used_plan": used_plan,
            "references": reference_manifest,
            "delivery": delivery,
            "agent_freedom": {
                "language_before_or_after": True,
                "other_tools_before_or_after": True,
                "continue_reasoning_after_call": True,
                "manual_redelivery_available": True,
            },
            "available_actions": {
                "continue_editing": "再次调用 generate_image，并把 genimg 提取码放入 refs；如需先看结果再交付可设 auto_send=false",
                "redeliver_original": "需要重发已有生成物时调用 send_generated_images",
                "continue_agent": "可继续自然语言、其他 Agent 工具或直接结束当前回复",
            },
        }
        content: list[TextContent | ImageContent] = [
            TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
        ]
        for image in generated:
            preview, mime = await self._preview(image.file_path)
            if preview:
                content.append(
                    ImageContent(
                        type="image",
                        data=base64.b64encode(preview).decode("ascii"),
                        mimeType=mime,
                    )
                )
        return CallToolResult(content=content, structuredContent=payload, isError=False)

    @filter.llm_tool(name="send_generated_images")
    async def send_generated_images(
        self,
        event: AstrMessageEvent,
        refs: list[str] | None = None,
    ) -> CallToolResult:
        """重发或补发已有 genimg: 生成物的原文件；不是 generate_image 的必经下一步。

        generate_image 默认已经自动发送。本工具保留为 Agent 基础设施：
        当 auto_send=false、自动发送部分失败、用户要求重发，或模型稍后决定交付历史生成物时可直接调用。

        Args:
            refs(list[string]): 一个或多个本插件 genimg: 提取码，按发送顺序填写。
        """
        normalized = _normalize_refs(refs)
        if not normalized:
            return _error_result("至少需要一个 genimg 提取码。")
        images = await asyncio.to_thread(self.store.resolve_many, _scope(event), normalized)
        existing = [image for image in images if image is not None]
        delivery = await self._deliver_images(event, existing)
        existing_iter = iter(delivery["results"])
        results = []
        for requested, image in zip(normalized, images, strict=True):
            if image is None:
                results.append({"ref": requested, "status": "unavailable"})
            else:
                results.append(next(existing_iter))
        payload = {
            "sent": delivery["sent"],
            "delivery": "original_file",
            "results": results,
        }
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            structuredContent=payload,
            isError=delivery["sent"] == 0,
        )

    @filter.llm_tool(name="list_image_capabilities")
    async def list_image_capabilities(self, event: AstrMessageEvent) -> str:
        """只读查询图片能力；可在任何模型步骤调用，不是生成前置条件。"""
        return json.dumps(
            {
                "plugin": PLUGIN_NAME,
                "version": VERSION,
                "models": self.client.models(),
                "aspects": ["landscape", "portrait", "square", "photo", "wide", "W:H"],
                "references": ["current", "genimg:<16hex>"],
                "reference_limits": {"max_images": 14, "max_bytes_each": MAX_REFERENCE_BYTES},
                "output_count": {"minimum": 1, "maximum": 15},
                "native_skill": "image-generation",
                "tool_policy": "direct_call_anytime",
                "delivery": {
                    "default": "automatic_original_file",
                    "defer_parameter": "auto_send=false",
                    "manual_tool": "send_generated_images",
                },
                "agent_harness": {
                    "forced_preamble": False,
                    "forced_followup": False,
                    "forced_tool_order": False,
                    "forced_final_text": False,
                },
                "independent_of": [
                    "astrbot_plugin_yangmo_core",
                    "astrbot_plugin_yangmo_qq_search",
                    "astrbot_plugin_group_context_image_locator",
                ],
            },
            ensure_ascii=False,
        )

    async def _deliver_images(
        self,
        event: AstrMessageEvent,
        images: list[GeneratedImage],
    ) -> dict:
        manifest = []
        sent = 0
        for image in images:
            try:
                message_id = await _send_original(event, image)
            except Exception as exc:
                logger.error(
                    "[yangmo.image] original send failed ref=%s", image.ref, exc_info=True
                )
                manifest.append(
                    {"ref": image.ref, "status": "send_failed", "error": str(exc)[:300]}
                )
                continue
            sent += 1
            manifest.append({"ref": image.ref, "status": "sent", "message_id": message_id})
        return {"sent": sent, "results": manifest}

    async def _resolve_references(self, event, refs: list[str]):
        scope = _scope(event)
        urls = []
        manifest = []
        seen_hashes = set()

        def append_reference(data: bytes, mime: str, source: dict) -> None:
            if len(data) > MAX_REFERENCE_BYTES:
                raise ValueError("单张参考图不能超过 30 MiB")
            if mime not in SUPPORTED_IMAGE_MIME:
                raise ValueError("参考图格式必须是 PNG、JPEG、WebP 或 GIF")
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen_hashes:
                manifest.append({**source, "status": "duplicate_ignored", "sha256": digest})
                return
            if len(urls) >= MAX_REFERENCE_IMAGES:
                raise ValueError("去重后的参考图不能超过 14 张")
            seen_hashes.add(digest)
            width, height = validate_reference_image(data, mime)
            urls.append(to_data_url(data, mime))
            manifest.append(
                {
                    **source,
                    "status": "resolved",
                    "sha256": digest,
                    "width": width,
                    "height": height,
                }
            )

        for ref in refs:
            if ref == "current":
                current = await self._current_images(event)
                for index, (data, mime) in enumerate(current):
                    append_reference(data, mime, {"ref": "current", "image_index": index})
                if not current:
                    manifest.append({"ref": "current", "status": "unavailable"})
                continue
            image = (await asyncio.to_thread(self.store.resolve_many, scope, [ref]))[0]
            if image is None:
                manifest.append({"ref": ref, "status": "unavailable"})
                continue
            if image.size > MAX_REFERENCE_BYTES:
                raise ValueError("单张参考图不能超过 30 MiB")
            data = await asyncio.to_thread(image.file_path.read_bytes)
            append_reference(data, image.mime_type, {"ref": ref})
        return urls, manifest

    async def _current_images(self, event) -> list[tuple[bytes, str]]:
        message_obj = getattr(event, "message_obj", None)
        chain = getattr(message_obj, "message", None)
        if hasattr(chain, "chain"):
            chain = chain.chain
        if not isinstance(chain, list):
            return []
        result = []
        for component in chain:
            if not isinstance(component, Image):
                continue
            path = Path(await component.convert_to_file_path())
            if path.stat().st_size > MAX_REFERENCE_BYTES:
                raise ValueError("单张参考图不能超过 30 MiB")
            data = await asyncio.to_thread(path.read_bytes)
            result.append((data, sniff_mime(data)))
        return result

    async def _preview(self, path: Path) -> tuple[bytes | None, str]:
        ffmpeg = str(self.config.get("ffmpeg_bin") or "").strip()
        if ffmpeg:
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    ffmpeg,
                    "-i",
                    str(path),
                    "-vf",
                    "scale=1024:1024:force_original_aspect_ratio=decrease",
                    "-f",
                    "mjpeg",
                    "-q:v",
                    "3",
                    "pipe:1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
                if process.returncode == 0 and stdout:
                    return stdout, "image/jpeg"
            except asyncio.TimeoutError:
                if process is not None and process.returncode is None:
                    process.kill()
                    await process.wait()
                logger.warning("[yangmo.image] preview timed out")
            except Exception as exc:
                logger.warning("[yangmo.image] preview unavailable: %s", exc)
        return None, "image/jpeg"

    async def _maybe_prune(self):
        now = time.monotonic()
        if now - self._last_cleanup < 3600 or self._cleanup_lock.locked():
            return
        async with self._cleanup_lock:
            if time.monotonic() - self._last_cleanup < 3600:
                return
            try:
                await asyncio.to_thread(
                    self.store.prune,
                    ttl_days=_bounded_int(self.config.get("generated_ttl_days"), 30, 1, 3650),
                    max_bytes=_bounded_int(
                        self.config.get("max_store_bytes"),
                        2 * 1024 * 1024 * 1024,
                        1024 * 1024,
                        100 * 1024 * 1024 * 1024,
                    ),
                )
            except Exception:
                logger.warning("[yangmo.image] cleanup failed; generation continues", exc_info=True)
            self._last_cleanup = time.monotonic()

    async def terminate(self):
        await asyncio.to_thread(self.store.close)


async def _send_original(event: AstrMessageEvent, image: GeneratedImage) -> str | None:
    suffix = image.file_path.suffix or ".bin"
    component = File(name=f"{image.ref.replace(':', '_')}{suffix}", file=str(image.file_path))
    chain = MessageChain(chain=[component])
    await event.send(chain)
    return None


def _error_result(message: str, *, api_calls: int = 0) -> CallToolResult:
    payload = {
        "status": "fail",
        "error": str(message)[:800],
        "api_calls": max(0, int(api_calls)),
    }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=payload,
        isError=True,
    )
