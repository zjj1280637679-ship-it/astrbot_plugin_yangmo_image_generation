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
VERSION = "0.2.0"
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
    ) -> CallToolResult:
        """直接生成、编辑或合成图片；任何模型步骤都可调用，无需 prepare 或任务句柄。

        契约：p ::= 非空完整图片指令；
        r ::= "current" | "genimg:" + 16 位十六进制；R ::= [] | [r1,...,rk]；1 <= n <= 15；
        generate(p,R,n,a) -> G，其中 G ::= [genimg:...,...]。
        工具调用会立即执行真实图片 API，请只在当前推理确实需要生成/编辑图片时调用。
        返回的 G 与内部 preview 供当前 AI 继续判断；它们不代表图片已经发送到聊天。
        count 只表达需要的图片数量；插件每个候选模型最多调用一次，不循环补画。
        当前候选零结果且属于额度/限流错误时才尝试下一模型；任何成功结果都会保留并停止降级。

        Args:
            prompt(string): p；完整画面描述或编辑指令。
            refs(list[string]): R；可选，仅接受 current 或本插件 genimg 提取码。
            count(number): n；需要的图片数量，范围 1..15（API 参数边界，不是插件配额）。
            aspect(string): a；landscape/portrait/square/photo/wide 或 W:H。
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
        payload = {
            "status": "ok",
            "generated": rows,
            "model": model,
            "api_calls": api_calls,
            "requested_count": image_count,
            "returned_count": len(rows),
            "used_plan": used_plan,
            "references": reference_manifest,
            "available_actions": {
                "deliver_original": "调用 send_generated_images 并传入 genimg 提取码",
                "continue_editing": "再次调用 generate_image，并把 genimg 提取码放入 refs",
                "finish_without_delivery": "如果当前任务只需要内部结果或无需发送，可直接继续回答",
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
        """把本插件 genimg 提取码对应的原图作为文件发送到当前聊天；可在任何模型步骤直接调用。

        契约：g ::= "genimg:" + 16 位十六进制；G ::= [g1,...,gn]，n >= 1；
        send(G) -> {delivery:"original_file", sent, results}。
        输入顺序=发送顺序；g 不属于外部插件提取码域、当前消息临时 ID、QQ file_id 或 "current"。
        本工具只发送已经存在的生成物，不要求之前执行固定生命周期。

        Args:
            refs(list[string]): G；按发送顺序填写本插件 genimg 提取码。
        """
        normalized = _normalize_refs(refs)
        if not normalized:
            return _error_result("至少需要一个 genimg 提取码。")
        images = await asyncio.to_thread(self.store.resolve_many, _scope(event), normalized)
        manifest = []
        sent = 0
        for requested, image in zip(normalized, images, strict=True):
            if image is None:
                manifest.append({"ref": requested, "status": "unavailable"})
                continue
            try:
                message_id = await _send_original(event, image)
            except Exception as exc:
                logger.error("[yangmo.image] original send failed ref=%s", requested, exc_info=True)
                manifest.append(
                    {"ref": requested, "status": "send_failed", "error": str(exc)[:300]}
                )
                continue
            sent += 1
            manifest.append(
                {"ref": requested, "status": "sent", "message_id": message_id}
            )
        payload = {"sent": sent, "delivery": "original_file", "results": manifest}
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            structuredContent=payload,
            isError=sent == 0,
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
                "delivery": "original_file",
                "independent_of": [
                    "astrbot_plugin_yangmo_core",
                    "astrbot_plugin_yangmo_qq_search",
                    "astrbot_plugin_group_context_image_locator",
                ],
            },
            ensure_ascii=False,
        )

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
