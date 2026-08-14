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
VERSION = "0.2.3"
MAX_REFERENCE_IMAGES = 14
MAX_REFERENCE_BYTES = 30 * 1024 * 1024
SUPPORTED_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}
DEFAULT_GENERATION_ANNOUNCEMENT = "收到，我开始处理图片，生成好就发给你。"


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
        announce: bool = True,
    ) -> CallToolResult:
        """生成、编辑或合成图片；默认先立即发一条简短开始通知，再执行生成，成功后自动发送原文件。

        这是可在任意 Agent 步骤直接调用的动作工具，没有 prepare、句柄或固定前后流程。
        `announce=true` 是低延迟 UX 默认值：工具一开始就告诉用户任务已接收，避免图片 API 等待期间没有反馈。
        如果 Agent 已经在调用前发过明确的开始通知，或用户明确要求只发图片/不要文字，可设 `announce=false` 避免重复。
        `auto_send=true` 省去一次机械发送步骤；当任务需要先比较候选、继续编辑、内部检查或择优交付时设为 false。
        无论是否自动发送，都会返回 `genimg:` 稳定引用和内部预览，供当前 Agent 后续继续推理或调用其他工具。
        每次调用都会执行真实外部图片 API，并可能产生费用；不要只为讨论、分析或规划图片而调用。

        Args:
            prompt(string): 完整而有信息密度的图片指令；不要为追求长度机械填充无意义细节。
            refs(list[string]): 可选；仅接受 current 或本插件 genimg: 提取码。
            count(number): 需要的图片数量，1..15（API 参数边界，不是插件配额）。
            aspect(string): landscape/portrait/square/photo/wide 或 W:H。
            auto_send(boolean): 默认 true；false 表示把交付时机留给 Agent。
            announce(boolean): 默认 true；立即发送简短开始通知。若 Agent 已自行预告或用户要求纯图片则设 false。
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

        if bool(announce):
            try:
                await event.send(MessageChain().message(DEFAULT_GENERATION_ANNOUNCEMENT))
            except Exception:
                logger.warning(
                    "[yangmo.image] generation announcement failed; generation continues",
                    exc_info=True,
                )

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
            "announcement": "sent" if bool(announce) else "suppressed",
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
        """发送或重发已有 genimg: 生成物的原文件。

        `generate_image` 默认已自动发送；本工具用于 `auto_send=false` 后择优交付、自动发送失败后的补发、用户要求重发，或稍后交付历史生成物。

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
        """只读查询图片能力；需要确认模型、边界或交付语义时调用。"""
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
                "interaction": {
                    "default_preamble": "automatic_short_notice",
                    "suppress_parameter": "announce=false",
                    "preamble_failure_blocks_generation": False,
                },
                "delivery": {
                    "default": "automatic_original_file",
                    "defer_parameter": "auto_send=false",
                    "manual_tool": "send_generated_images",
                },
                "prompt_guidance": {
                    "hard_limit_known": False,
                    "strategy": "concise_semantically_dense_natural_language",
                    "do_not_pad_to_percentage": True,
                    "note": "当前插件没有可靠的模型级硬提示词上限；优先表达有效约束，不按假定上限机械填充。",
                },
                "agent_harness": {
                    "default_preamble": True,
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
