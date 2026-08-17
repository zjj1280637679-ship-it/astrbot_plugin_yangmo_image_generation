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
from astrbot.api.message_components import File, Image, Video
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
from .video import ArkVideoClient, VideoApiError, VideoConfigError
from .video_store import GeneratedFrame, GeneratedVideo, GeneratedVideoStore

PLUGIN_NAME = "astrbot_plugin_yangmo_image_generation"
VERSION = "0.3.0"
MAX_REFERENCE_IMAGES = 14
MAX_REFERENCE_BYTES = 30 * 1024 * 1024
SUPPORTED_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}
DEFAULT_IMAGE_ANNOUNCEMENT = "收到，我开始处理图片，生成好就发给你。"
DEFAULT_VIDEO_ANNOUNCEMENT = "收到，我开始生成视频，完成后直接发给你。"
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
        self.video_store = GeneratedVideoStore(root)
        self.client = ArkImageClient(config)
        self.video_client = ArkVideoClient(config)
        self._cleanup_lock = asyncio.Lock()
        self._last_cleanup = 0.0
        logger.info("[yangmo.generation] ready version=%s store=%s", VERSION, root)

    @filter.on_llm_tool_respond(priority=-100)
    async def capture_recent_tool_images(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict | None,
        tool_result: CallToolResult | None,
    ) -> None:
        """Remember public images returned by other tools for this event only.

        This never imports another plugin or reads its storage. It deliberately ignores this
        plugin's own generation/delivery tools, so `resolved` means an image obtained from an
        external resolver/search/tool rather than our own preview.
        """
        if tool_result is None:
            return
        tool_name = str(getattr(tool, "name", "") or "")
        if tool_name in {
            "generate_image",
            "send_generated_images",
            "generate_video",
            "send_generated_videos",
            "list_image_capabilities",
            "list_generation_capabilities",
        }:
            return
        cached: list[tuple[bytes, str]] = []
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
            cached.append((data, mime))
        if cached:
            event.set_extra(TOOL_IMAGE_CACHE_KEY, cached[:MAX_REFERENCE_IMAGES])

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
        """生成、编辑或合成图片；默认先通知、成功后自动发送原文件。

        Args:
            prompt(string): 完整而有信息密度的图片指令。
            refs(list[string]): 可选；current 或本插件 genimg: 提取码。
            count(number): 需要的图片数量，1..15。
            aspect(string): landscape/portrait/square/photo/wide 或 W:H。
            auto_send(boolean): 默认 true；false 把交付时机留给 Agent。
            announce(boolean): 默认 true；若已预告或用户要求纯图片可设 false。
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
            await self._safe_announce(event, DEFAULT_IMAGE_ANNOUNCEMENT, "image")

        try:
            reference_urls, reference_manifest = await self._resolve_references(event, _normalize_refs(refs))
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
            generated: list[GeneratedImage] = []
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
            {"ref": image.ref, "mime_type": image.mime_type, "bytes": image.size, "sha256": image.sha256}
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
        content: list[TextContent | ImageContent] = [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
        for image in generated:
            preview, mime = await self._preview(image.file_path)
            if preview:
                content.append(ImageContent(type="image", data=base64.b64encode(preview).decode("ascii"), mimeType=mime))
        return CallToolResult(content=content, structuredContent=payload, isError=False)

    @filter.llm_tool(name="send_generated_images")
    async def send_generated_images(self, event: AstrMessageEvent, refs: list[str] | None = None) -> CallToolResult:
        """发送或重发已有 genimg: 生成物的原文件。

        Args:
            refs(list[string]): 一个或多个本插件 genimg: 提取码。
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
            results.append({"ref": requested, "status": "unavailable"} if image is None else next(existing_iter))
        payload = {"sent": delivery["sent"], "delivery": "original_file", "results": results}
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            structuredContent=payload,
            isError=delivery["sent"] == 0,
        )

    @filter.llm_tool(name="generate_video")
    async def generate_video(
        self,
        event: AstrMessageEvent,
        prompt: str,
        first_frame: str = "",
        duration: int = 5,
        ratio: str = "adaptive",
        resolution: str = "1080p",
        return_last_frame: bool = True,
        auto_send: bool = True,
        announce: bool = True,
    ) -> CallToolResult:
        """使用 Seedance 生成视频；支持文生视频和单首帧图生视频，默认先通知并自动发送视频。

        本工具独立可用：first_frame 为空就是文生视频，current 使用当前消息图片。
        与图片定位/搜索插件共存时，不读取对方内部状态；对方工具先把原图返回给 Agent 后，使用 resolved 即可。
        与本插件图片生成共存时，可直接使用 genimg:；上一次视频返回的 genframe: 也可作为下一段首帧。

        Args:
            prompt(string): 视频动作、时间顺序、镜头运动、环境变化和连续性要求。
            first_frame(string): 可选；current、resolved、genimg:... 或 genframe:...。留空为文生视频。
            duration(number): 5 或 10 秒。
            ratio(string): adaptive、1:1、16:9、4:3、21:9、9:16、3:4。
            resolution(string): 480p、720p 或 1080p；默认 1080p。
            return_last_frame(boolean): 默认 true；保存返回尾帧为 genframe:，便于续接下一段。
            auto_send(boolean): 默认 true；false 用于候选比较/择优交付。
            announce(boolean): 默认 true；已预告或纯结果任务可设 false。
        """
        prompt_text = str(prompt or "").strip()
        if not prompt_text:
            return _error_result("视频 prompt 不能为空。")
        try:
            duration_value = int(duration)
        except (TypeError, ValueError):
            return _error_result("duration 必须是整数。")
        ratio_value = str(ratio or "adaptive").strip().lower()
        resolution_value = str(resolution or "1080p").strip().lower()

        if bool(announce):
            await self._safe_announce(event, DEFAULT_VIDEO_ANNOUNCEMENT, "video")

        try:
            first_frame_url, frame_manifest = await self._resolve_video_first_frame(event, first_frame)
            self.video_client.preflight(prompt_text, ratio_value, duration_value, resolution_value)
            await self._maybe_prune()
            task_id = await self.video_client.create_task(
                prompt=prompt_text,
                ratio=ratio_value,
                duration=duration_value,
                resolution=resolution_value,
                first_frame_data_url=first_frame_url,
                return_last_frame=bool(return_last_frame),
            )
            task, api_calls = await self.video_client.wait_task(task_id)
            video_url, last_frame_url = self.video_client.output_urls(task)
            if not video_url:
                return _error_result(f"视频任务成功但未返回 video_url（task_id={task_id}）。", api_calls=api_calls)
            max_video_bytes = _bounded_int(
                self.config.get("max_video_download_mb"), 256, 16, 2048
            ) * 1024 * 1024
            video_data = await self.video_client.download(video_url, max_bytes=max_video_bytes)
            video = await asyncio.to_thread(
                self.video_store.put_video,
                scope=_scope(event),
                data=video_data,
                task_id=task_id,
            )
            frame: GeneratedFrame | None = None
            if bool(return_last_frame) and last_frame_url:
                try:
                    frame_data = await self.video_client.download(last_frame_url, max_bytes=MAX_REFERENCE_BYTES)
                    frame_mime = sniff_mime(frame_data)
                    if frame_mime in SUPPORTED_IMAGE_MIME:
                        frame = await asyncio.to_thread(
                            self.video_store.put_frame,
                            scope=_scope(event),
                            data=frame_data,
                            mime_type=frame_mime,
                            task_id=task_id,
                        )
                except Exception:
                    logger.warning("[yangmo.video] last-frame download failed task_id=%s", task_id, exc_info=True)
        except VideoApiError as exc:
            return _error_result(str(exc), api_calls=exc.api_calls, task_id=exc.task_id)
        except VideoConfigError as exc:
            return _error_result(str(exc), api_calls=0)
        except Exception as exc:
            logger.error("[yangmo.video] generation failed", exc_info=True)
            return _error_result(f"视频生成失败：{str(exc)[:300]}")

        delivery = {"mode": "deferred", "sent": 0, "results": []}
        if bool(auto_send):
            delivery = await self._deliver_videos(event, [video])
            delivery["mode"] = "automatic"
        payload = {
            "status": "ok",
            "generated": {
                "ref": video.ref,
                "mime_type": video.mime_type,
                "bytes": video.size,
                "sha256": video.sha256,
                "task_id": task_id,
            },
            "last_frame": (
                {"ref": frame.ref, "mime_type": frame.mime_type, "bytes": frame.size, "sha256": frame.sha256}
                if frame else None
            ),
            "model": str(task.get("model") or self.video_client.model()),
            "api_calls": api_calls,
            "task_status": str(task.get("status") or "succeeded"),
            "requested": {
                "duration": duration_value,
                "ratio": ratio_value,
                "resolution": resolution_value,
            },
            "actual": {
                "duration": task.get("duration"),
                "ratio": task.get("ratio"),
                "resolution": task.get("resolution"),
                "frames": task.get("frames"),
                "frames_per_second": task.get("framespersecond") or task.get("frames_per_second"),
            },
            "first_frame": frame_manifest,
            "delivery": delivery,
            "announcement": "sent" if bool(announce) else "suppressed",
        }
        content: list[TextContent | ImageContent] = [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
        preview_data: bytes | None = None
        preview_mime = "image/jpeg"
        if frame is not None:
            try:
                preview_data = await asyncio.to_thread(frame.file_path.read_bytes)
                preview_mime = frame.mime_type
            except OSError:
                preview_data = None
        if preview_data is None:
            preview_data, preview_mime = await self._video_preview(video.file_path)
        if preview_data:
            content.append(ImageContent(type="image", data=base64.b64encode(preview_data).decode("ascii"), mimeType=preview_mime))
        return CallToolResult(content=content, structuredContent=payload, isError=False)

    @filter.llm_tool(name="send_generated_videos")
    async def send_generated_videos(self, event: AstrMessageEvent, refs: list[str] | None = None) -> CallToolResult:
        """发送或重发已有 genvideo: 视频。

        Args:
            refs(list[string]): 一个或多个本插件 genvideo: 提取码。
        """
        normalized = _normalize_refs(refs)
        if not normalized:
            return _error_result("至少需要一个 genvideo 提取码。")
        videos = await asyncio.to_thread(self.video_store.resolve_videos, _scope(event), normalized)
        existing = [video for video in videos if video is not None]
        delivery = await self._deliver_videos(event, existing)
        existing_iter = iter(delivery["results"])
        results = []
        for requested, video in zip(normalized, videos, strict=True):
            results.append({"ref": requested, "status": "unavailable"} if video is None else next(existing_iter))
        payload = {"sent": delivery["sent"], "delivery": "video", "results": results}
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            structuredContent=payload,
            isError=delivery["sent"] == 0,
        )

    @filter.llm_tool(name="list_generation_capabilities")
    async def list_generation_capabilities(self, event: AstrMessageEvent) -> str:
        """只读查询图片与视频生成能力；不是任何生成动作的前置步骤。"""
        return json.dumps(
            {
                "plugin": PLUGIN_NAME,
                "version": VERSION,
                "image": {
                    "models": self.client.models(),
                    "aspects": ["landscape", "portrait", "square", "photo", "wide", "W:H"],
                    "references": ["current", "genimg:<16hex>"],
                    "output_count": {"minimum": 1, "maximum": 15},
                    "default_announce": True,
                    "default_auto_send": True,
                },
                "video": {
                    "model": self.video_client.model(),
                    "modes": ["text_to_video", "first_frame_image_to_video"],
                    "first_frame_sources": ["current", "resolved", "genimg:<16hex>", "genframe:<16hex>"],
                    "ratios": ["adaptive", "1:1", "16:9", "4:3", "21:9", "9:16", "3:4"],
                    "durations_seconds": [5, 10],
                    "resolutions": ["480p", "720p", "1080p"],
                    "return_last_frame": True,
                    "default_announce": True,
                    "default_auto_send": True,
                    "async_task_api": True,
                    "first_last_frame_constraint": "not_exposed_in_v0.3.0",
                },
                "interop": {
                    "strategy": "public_tool_content_not_foreign_storage",
                    "resolved": "最近一个外部工具返回的 ImageContent；适合先调用群聊图片定位/搜索工具，再做首帧图生视频。",
                    "imports_other_plugins": False,
                },
                "agent_harness": {
                    "forced_tool_order": False,
                    "forced_final_text": False,
                    "skill_is_permission_gate": False,
                },
            },
            ensure_ascii=False,
        )

    @filter.llm_tool(name="list_image_capabilities")
    async def list_image_capabilities(self, event: AstrMessageEvent) -> str:
        """兼容旧调用；返回图片能力。"""
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
                "interaction": {"default_preamble": "automatic_short_notice", "suppress_parameter": "announce=false"},
                "delivery": {"default": "automatic_original_file", "defer_parameter": "auto_send=false"},
            },
            ensure_ascii=False,
        )

    async def _safe_announce(self, event: AstrMessageEvent, text: str, kind: str) -> None:
        try:
            await event.send(MessageChain().message(text))
        except Exception:
            logger.warning("[yangmo.%s] announcement failed; generation continues", kind, exc_info=True)

    async def _deliver_images(self, event: AstrMessageEvent, images: list[GeneratedImage]) -> dict:
        manifest = []
        sent = 0
        for image in images:
            try:
                message_id = await _send_original(event, image)
            except Exception as exc:
                logger.error("[yangmo.image] original send failed ref=%s", image.ref, exc_info=True)
                manifest.append({"ref": image.ref, "status": "send_failed", "error": str(exc)[:300]})
                continue
            sent += 1
            manifest.append({"ref": image.ref, "status": "sent", "message_id": message_id})
        return {"sent": sent, "results": manifest}

    async def _deliver_videos(self, event: AstrMessageEvent, videos: list[GeneratedVideo]) -> dict:
        manifest = []
        sent = 0
        for video in videos:
            try:
                component = Video.fromFileSystem(path=str(video.file_path))
                await event.send(MessageChain(chain=[component]))
            except Exception as exc:
                logger.error("[yangmo.video] send failed ref=%s", video.ref, exc_info=True)
                manifest.append({"ref": video.ref, "status": "send_failed", "error": str(exc)[:300]})
                continue
            sent += 1
            manifest.append({"ref": video.ref, "status": "sent", "message_id": None})
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
            manifest.append({**source, "status": "resolved", "sha256": digest, "width": width, "height": height})

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
            data = await asyncio.to_thread(image.file_path.read_bytes)
            append_reference(data, image.mime_type, {"ref": ref})
        return urls, manifest

    async def _resolve_video_first_frame(self, event: AstrMessageEvent, ref: str) -> tuple[str | None, dict]:
        value = str(ref or "").strip()
        if not value:
            return None, {"mode": "text_to_video", "status": "not_used"}
        scope = _scope(event)
        data: bytes | None = None
        mime = ""
        source = value
        if value == "current":
            images = await self._current_images(event)
            if images:
                data, mime = images[0]
        elif value in {"resolved", "tool", "recent_tool"}:
            images = event.get_extra(TOOL_IMAGE_CACHE_KEY, []) or []
            if images:
                data, mime = images[0]
        elif value.startswith("genimg:"):
            image = (await asyncio.to_thread(self.store.resolve_many, scope, [value]))[0]
            if image is not None:
                data = await asyncio.to_thread(image.file_path.read_bytes)
                mime = image.mime_type
        elif value.startswith("genframe:"):
            frame = (await asyncio.to_thread(self.video_store.resolve_frames, scope, [value]))[0]
            if frame is not None:
                data = await asyncio.to_thread(frame.file_path.read_bytes)
                mime = frame.mime_type
        else:
            raise ValueError("first_frame 仅支持 current、resolved、genimg: 或 genframe:。")
        if not data:
            raise ValueError(f"首帧不可用：{source}")
        if len(data) > MAX_REFERENCE_BYTES:
            raise ValueError("首帧图片不能超过 30 MiB")
        if mime not in SUPPORTED_IMAGE_MIME:
            mime = sniff_mime(data)
        validate_reference_image(data, mime)
        digest = hashlib.sha256(data).hexdigest()
        return to_data_url(data, mime), {"mode": "image_to_video", "source": source, "status": "resolved", "sha256": digest}

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
                    ffmpeg, "-i", str(path), "-vf", "scale=1024:1024:force_original_aspect_ratio=decrease",
                    "-f", "mjpeg", "-q:v", "3", "pipe:1",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
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

    async def _video_preview(self, path: Path) -> tuple[bytes | None, str]:
        ffmpeg = str(self.config.get("ffmpeg_bin") or "").strip()
        if not ffmpeg:
            return None, "image/jpeg"
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                ffmpeg, "-ss", "1", "-i", str(path), "-frames:v", "1",
                "-vf", "scale=1024:1024:force_original_aspect_ratio=decrease",
                "-f", "mjpeg", "-q:v", "3", "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
            if process.returncode == 0 and stdout:
                return stdout, "image/jpeg"
        except asyncio.TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
        except Exception as exc:
            logger.warning("[yangmo.video] preview unavailable: %s", exc)
        return None, "image/jpeg"

    async def _maybe_prune(self):
        now = time.monotonic()
        if now - self._last_cleanup < 3600 or self._cleanup_lock.locked():
            return
        async with self._cleanup_lock:
            if time.monotonic() - self._last_cleanup < 3600:
                return
            try:
                ttl_days = _bounded_int(self.config.get("generated_ttl_days"), 30, 1, 3650)
                image_max = _bounded_int(
                    self.config.get("max_store_bytes"), 2 * 1024 * 1024 * 1024,
                    1024 * 1024, 100 * 1024 * 1024 * 1024,
                )
                video_max = _bounded_int(
                    self.config.get("max_video_store_bytes"), 8 * 1024 * 1024 * 1024,
                    16 * 1024 * 1024, 200 * 1024 * 1024 * 1024,
                )
                await asyncio.to_thread(self.store.prune, ttl_days=ttl_days, max_bytes=image_max)
                await asyncio.to_thread(self.video_store.prune, ttl_days=ttl_days, max_bytes=video_max)
            except Exception:
                logger.warning("[yangmo.generation] cleanup failed; generation continues", exc_info=True)
            self._last_cleanup = time.monotonic()

    async def terminate(self):
        await asyncio.to_thread(self.store.close)
        await asyncio.to_thread(self.video_store.close)


async def _send_original(event: AstrMessageEvent, image: GeneratedImage) -> str | None:
    # Image mime types go out as inline images (aiocqhttp converts Image components to
    # base64 `image` segments, which WeCat delivers as WeChat images). Non-image payloads
    # keep the File component path.
    if (image.mime_type or "").startswith("image/"):
        component = Image.fromFileSystem(path=str(image.file_path))
    else:
        suffix = image.file_path.suffix or ".bin"
        component = File(name=f"{image.ref.replace(':', '_')}{suffix}", file=str(image.file_path))
    await event.send(MessageChain(chain=[component]))
    return None


def _error_result(message: str, *, api_calls: int = 0, task_id: str = "") -> CallToolResult:
    payload = {"status": "fail", "error": str(message)[:800], "api_calls": max(0, int(api_calls))}
    if task_id:
        payload["task_id"] = task_id
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=payload,
        isError=True,
    )
