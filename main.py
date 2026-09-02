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
from astrbot.api.message_components import Image
from astrbot.api.star import StarTools
from mcp.types import CallToolResult, ImageContent, TextContent

from .api import (
    ArkImageClient,
    ImageApiError,
    ImageConfigError,
    SEEDREAM_5_PRO_MAX_REFS,
    aspect_to_size,
    normalize_reference_format,
    sniff_mime,
    to_data_url,
    validate_reference_image,
)
from .store import GeneratedImage, GeneratedImageStore

PLUGIN_NAME = "astrbot_plugin_yangmo_image_generation"
VERSION = "0.4.0"
MAX_REFERENCE_IMAGES = SEEDREAM_5_PRO_MAX_REFS
MAX_REFERENCE_BYTES = 30 * 1024 * 1024
MAX_TOTAL_REFERENCE_BYTES = 120 * 1024 * 1024
SUPPORTED_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}
DEFAULT_IMAGE_ANNOUNCEMENT = "收到，我开始处理图片，生成好就发给你。"
TOOL_IMAGE_CACHE_KEY = "_yangmo_generation_recent_tool_images"


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

    @filter.on_llm_tool_respond(priority=-100)
    async def capture_recent_tool_images(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict | None,
        tool_result: CallToolResult | None,
    ) -> None:
        """Cache public ImageContent returned by the most recent external tool."""
        del tool_args
        if tool_result is None:
            return
        tool_name = str(getattr(tool, "name", "") or "")
        if tool_name in {
            "generate_image",
            "send_generated_images",
            "list_image_capabilities",
        }:
            return
        cached: list[tuple[bytes, str]] = []
        total_bytes = 0
        for item in list(getattr(tool_result, "content", []) or []):
            if not isinstance(item, ImageContent):
                continue
            try:
                data = base64.b64decode(str(item.data), validate=True)
            except Exception:
                continue
            mime = str(getattr(item, "mimeType", "") or sniff_mime(data)).lower()
            if mime not in SUPPORTED_IMAGE_MIME or not data:
                continue
            if len(data) > MAX_REFERENCE_BYTES:
                continue
            if total_bytes + len(data) > MAX_TOTAL_REFERENCE_BYTES:
                break
            cached.append((data, mime))
            total_bytes += len(data)
            if len(cached) >= MAX_REFERENCE_IMAGES:
                break
        if cached:
            event.set_extra(TOOL_IMAGE_CACHE_KEY, cached)

    @filter.llm_tool(name="generate_image")
    async def generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
        refs: list[str] | None = None,
        aspect: str = "landscape",
        auto_send: bool = True,
        announce: bool = True,
    ) -> CallToolResult:
        """生成或编辑一张图片。画图、改图、重绘、合成、扩图、海报或角色图请求应选本工具；不要用于搜索已有图片。默认先短通知并自动发送原图。

        Args:
            prompt(string): 完整生图指令；应包含主体、关系、构图和必须保持的约束。
            refs(list[string]): 可选参考图来源；current、resolved 或 genimg: 提取码，可按顺序组合，最多 10 张。
            aspect(string): 构图画幅。可用 landscape、portrait、square、photo、wide、W:H；仅当用户明确指定像素时使用 WIDTHxHEIGHT。
            auto_send(boolean): true 自动交付；false 仅保存并返回 genimg，供比较、继续编辑或稍后发送。
            announce(boolean): true 在耗时操作前发送短通知；已预告或用户要求只发图片时设 false。
        """
        prompt_text = str(prompt or "").strip()
        if not prompt_text:
            return _error_result("prompt 不能为空。")
        normalized_refs = _normalize_refs(refs)
        if len(normalized_refs) > MAX_REFERENCE_IMAGES:
            return _error_result(f"参考图来源不能超过 {MAX_REFERENCE_IMAGES} 个。")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.client.total_budget()

        announcement_sent = False
        if bool(announce):
            announcement_sent = await self._safe_announce(event, DEFAULT_IMAGE_ANNOUNCEMENT)

        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ImageConfigError("图片生成总时长预算已在准备阶段用尽")
            reference_urls, reference_manifest = await asyncio.wait_for(
                self._resolve_references(event, normalized_refs), timeout=remaining
            )
            size_request = aspect_to_size(aspect, self.config)
            self.client.preflight(prompt_text, size_request, reference_urls)
            await self._maybe_prune()
            (
                urls,
                api_calls,
                model,
                used_plan,
                route,
                effective_size,
            ) = await self.client.generate(
                prompt_text,
                size_request,
                reference_urls,
                deadline=deadline,
            )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ImageApiError(
                    "图片生成完成，但总时长预算已用尽，未开始下载结果",
                    api_calls=api_calls,
                )
            try:
                data = await asyncio.wait_for(
                    self.client.download(urls[0]), timeout=remaining
                )
            except asyncio.TimeoutError as exc:
                raise ImageApiError(
                    "图片结果下载超过本次工具调用总时长预算",
                    api_calls=api_calls,
                ) from exc
            except ImageApiError as exc:
                exc.api_calls = api_calls
                raise
            mime = sniff_mime(data)
            if mime not in SUPPORTED_IMAGE_MIME:
                raise ValueError("下载结果不是受支持的图片格式")
            generated = await asyncio.to_thread(
                self.store.put,
                scope=_scope(event),
                data=data,
                mime_type=mime,
            )
        except asyncio.TimeoutError:
            return _error_result("参考图解析超过图片生成链总时长预算")
        except ImageApiError as exc:
            return _error_result(str(exc), api_calls=exc.api_calls)
        except ImageConfigError as exc:
            return _error_result(str(exc), api_calls=exc.api_calls)
        except ValueError as exc:
            return _error_result(str(exc))
        except Exception as exc:
            logger.error("[yangmo.image] generation failed", exc_info=True)
            return _error_result(f"生成失败：{str(exc)[:300]}")

        delivery = {"mode": "deferred", "sent": 0, "results": []}
        if bool(auto_send):
            delivery = await self._deliver_images(event, [generated])
            delivery["mode"] = "automatic"

        status = "ok"
        is_error = False
        if bool(auto_send) and delivery["sent"] == 0:
            status = "generated_but_delivery_failed"
            is_error = True
        row = {
            "ref": generated.ref,
            "mime_type": generated.mime_type,
            "bytes": generated.size,
            "sha256": generated.sha256,
        }
        payload = {
            "status": status,
            "generated": [row],
            "model": model,
            "route": route,
            "used_plan": used_plan,
            "api_calls": api_calls,
            "requested": {
                "aspect": str(aspect or "landscape"),
                "size_mode": size_request.mode,
            },
            "effective_size": effective_size,
            "references": reference_manifest,
            "delivery": delivery,
            "announcement": (
                "sent" if announcement_sent else "failed" if bool(announce) else "suppressed"
            ),
        }
        content: list[TextContent | ImageContent] = [
            TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
        ]
        preview, preview_mime = await self._preview(generated.file_path)
        if preview:
            content.append(
                ImageContent(
                    type="image",
                    data=base64.b64encode(preview).decode("ascii"),
                    mimeType=preview_mime,
                )
            )
        return CallToolResult(
            content=content,
            structuredContent=payload,
            isError=is_error,
        )

    @filter.llm_tool(name="send_generated_images")
    async def send_generated_images(
        self, event: AstrMessageEvent, refs: list[str] | None = None
    ) -> CallToolResult:
        """只发送已存在的 genimg 图片，不生成新图。用户要求补发、重发或交付候选图时选择本工具。

        Args:
            refs(list[string]): 要发送的一个或多个 genimg: 提取码；按给定顺序发送。
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
            results.append(
                {"ref": requested, "status": "unavailable"}
                if image is None
                else next(existing_iter)
            )
        sent = delivery["sent"]
        status = "ok" if sent == len(normalized) else "partial" if sent else "fail"
        payload = {
            "status": status,
            "sent": sent,
            "delivery": "original_image",
            "results": results,
        }
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            structuredContent=payload,
            isError=sent == 0,
        )

    @filter.llm_tool(name="list_image_capabilities")
    async def list_image_capabilities(self, event: AstrMessageEvent) -> str:
        """只读查询本插件的图片模型卡、画幅、引用和交付能力。仅在用户询问能力或限制时选择；生成请求不要先调用。"""
        del event
        return json.dumps(
            {
                "plugin": PLUGIN_NAME,
                "version": VERSION,
                "model_cards": self.client.public_cards(),
                "models": self.client.models(),
                "output_images_per_call": 1,
                "aspects": [
                    "landscape",
                    "portrait",
                    "square",
                    "photo",
                    "wide",
                    "W:H",
                    "WIDTHxHEIGHT",
                ],
                "references": ["current", "resolved", "genimg:<16hex>"],
                "reference_limits": {
                    "max_images": MAX_REFERENCE_IMAGES,
                    "max_bytes_each": MAX_REFERENCE_BYTES,
                    "max_bytes_total": MAX_TOTAL_REFERENCE_BYTES,
                },
                "native_skill": "image-generation",
                "tool_schema_modes": ["full", "skills_like_two_stage"],
                "delivery": {
                    "default": "automatic_original_image",
                    "defer_parameter": "auto_send=false",
                },
            },
            ensure_ascii=False,
        )

    async def _safe_announce(self, event: AstrMessageEvent, text: str) -> bool:
        try:
            await event.send(MessageChain().message(text))
            return True
        except Exception:
            logger.warning("[yangmo.image] announcement failed; generation continues", exc_info=True)
            return False

    async def _deliver_images(
        self, event: AstrMessageEvent, images: list[GeneratedImage]
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

    async def _resolve_references(
        self, event: AstrMessageEvent, refs: list[str]
    ) -> tuple[list[str], list[dict]]:
        scope = _scope(event)
        urls: list[str] = []
        manifest: list[dict] = []
        seen_hashes: set[str] = set()
        total_bytes = 0

        def append_reference(data: bytes, mime: str, source: dict) -> None:
            nonlocal total_bytes
            if len(data) > MAX_REFERENCE_BYTES:
                raise ValueError("单张参考图不能超过 30 MiB")
            if mime not in SUPPORTED_IMAGE_MIME:
                raise ValueError("参考图格式必须是 PNG、JPEG、WebP 或 GIF")
            validate_reference_image(data, mime)
            normalized_data, normalized_mime = normalize_reference_format(data, mime)
            if len(normalized_data) > MAX_REFERENCE_BYTES:
                raise ValueError("参考图转换后超过 30 MiB")
            digest = hashlib.sha256(normalized_data).hexdigest()
            if digest in seen_hashes:
                manifest.append({**source, "status": "duplicate_ignored", "sha256": digest})
                return
            if len(urls) >= MAX_REFERENCE_IMAGES:
                raise ValueError(f"去重后的参考图不能超过 {MAX_REFERENCE_IMAGES} 张")
            if total_bytes + len(normalized_data) > MAX_TOTAL_REFERENCE_BYTES:
                raise ValueError("参考图总大小不能超过 120 MiB")
            seen_hashes.add(digest)
            total_bytes += len(normalized_data)
            width, height = validate_reference_image(normalized_data, normalized_mime)
            urls.append(to_data_url(normalized_data, normalized_mime))
            manifest.append(
                {
                    **source,
                    "status": "resolved",
                    "sha256": digest,
                    "width": width,
                    "height": height,
                    "mime_type": normalized_mime,
                    "converted_from": mime if mime != normalized_mime else None,
                }
            )

        for ref in refs:
            if ref == "current":
                current = await self._current_images(event)
                if not current:
                    raise ValueError("current 没有可用图片")
                for index, (data, mime) in enumerate(current):
                    append_reference(data, mime, {"ref": "current", "image_index": index})
                continue
            if ref in {"resolved", "tool", "recent_tool"}:
                resolved = list(event.get_extra(TOOL_IMAGE_CACHE_KEY, []) or [])
                if not resolved:
                    raise ValueError("resolved 没有最近外部工具返回的图片")
                for index, (data, mime) in enumerate(resolved):
                    append_reference(data, mime, {"ref": "resolved", "image_index": index})
                continue
            if not ref.startswith("genimg:"):
                raise ValueError(f"不支持的参考图来源：{ref}")
            image = (await asyncio.to_thread(self.store.resolve_many, scope, [ref]))[0]
            if image is None:
                raise ValueError(f"参考图不可用：{ref}")
            data = await asyncio.to_thread(image.file_path.read_bytes)
            append_reference(data, image.mime_type, {"ref": ref})
        return urls, manifest

    async def _current_images(self, event: AstrMessageEvent) -> list[tuple[bytes, str]]:
        message_obj = getattr(event, "message_obj", None)
        chain = getattr(message_obj, "message", None)
        if hasattr(chain, "chain"):
            chain = chain.chain
        if not isinstance(chain, list):
            return []
        result: list[tuple[bytes, str]] = []
        total_bytes = 0
        for component in chain:
            if not isinstance(component, Image):
                continue
            path = Path(await component.convert_to_file_path())
            size = path.stat().st_size
            if size > MAX_REFERENCE_BYTES:
                raise ValueError("单张参考图不能超过 30 MiB")
            if total_bytes + size > MAX_TOTAL_REFERENCE_BYTES:
                raise ValueError("参考图总大小不能超过 120 MiB")
            data = await asyncio.to_thread(path.read_bytes)
            mime = sniff_mime(data)
            if mime not in SUPPORTED_IMAGE_MIME:
                raise ValueError("当前消息包含不支持的图片格式")
            result.append((data, mime))
            total_bytes += len(data)
            if len(result) >= MAX_REFERENCE_IMAGES:
                break
        return result

    async def _preview(self, path: Path) -> tuple[bytes | None, str]:
        ffmpeg = str(self.config.get("ffmpeg_bin") or "").strip()
        if not ffmpeg:
            return None, "image/jpeg"
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
        except Exception as exc:
            logger.warning("[yangmo.image] preview unavailable: %s", exc)
        return None, "image/jpeg"

    async def _maybe_prune(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < 3600 or self._cleanup_lock.locked():
            return
        async with self._cleanup_lock:
            if time.monotonic() - self._last_cleanup < 3600:
                return
            try:
                ttl_days = _bounded_int(
                    self.config.get("generated_ttl_days"), 30, 1, 3650
                )
                image_max = _bounded_int(
                    self.config.get("max_store_bytes"),
                    2 * 1024 * 1024 * 1024,
                    1024 * 1024,
                    100 * 1024 * 1024 * 1024,
                )
                await asyncio.to_thread(
                    self.store.prune, ttl_days=ttl_days, max_bytes=image_max
                )
            except Exception:
                logger.warning("[yangmo.image] cleanup failed; generation continues", exc_info=True)
            self._last_cleanup = time.monotonic()

    async def terminate(self) -> None:
        await asyncio.to_thread(self.store.close)


async def _send_original(event: AstrMessageEvent, image: GeneratedImage) -> str | None:
    component = Image.fromFileSystem(path=str(image.file_path))
    await event.send(MessageChain(chain=[component]))
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
