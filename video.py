from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import aiohttp


class VideoConfigError(ValueError):
    pass


class VideoApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        api_calls: int = 1,
        task_id: str = "",
        fallback_allowed: bool = False,
    ):
        super().__init__(message)
        self.api_calls = max(0, int(api_calls))
        self.task_id = str(task_id or "")
        self.fallback_allowed = bool(fallback_allowed)


SUPPORTED_RATIOS = {"adaptive", "1:1", "16:9", "4:3", "21:9", "9:16", "3:4"}
SUPPORTED_DURATIONS = {5, 10}
SUPPORTED_RESOLUTIONS = {"480p", "720p", "1080p"}
_ZHIPU_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_ZHIPU_MODEL_DEFAULT = "cogvideox-flash"
_ZHIPU_BASE_URL_DEFAULT = "https://open.bigmodel.cn/api/paas/v4"


def _recoverable_provider_error(status: int | None, code: str, message: str) -> bool:
    if status in {408, 429}:
        return True
    if status is not None and 500 <= status <= 599:
        return True
    text = f"{code} {message}".lower().replace("_", "").replace("-", "")
    needles = (
        "ratelimit",
        "toomanyrequests",
        "quota",
        "insufficientbalance",
        "accountoverdue",
        "overload",
        "serverbusy",
        "serviceunavailable",
        "capacity",
        "throttl",
    )
    return any(item in text for item in needles)


class ArkVideoClient:
    """Seedance primary with optional CogVideoX-Flash provider rotation.

    Keep the v0.3.0 public interface so the AstrBot harness does not need a
    second provider-specific tool. Rotation only occurs before Seedance has
    accepted a task. Explicit quota/rate/overload/server responses may rotate;
    ambiguous timeout/network failures do not, avoiding accidental duplicate
    paid submissions.
    """

    def __init__(self, config: dict):
        self.config = config

    def model(self) -> str:
        return str(
            self.config.get("video_model") or "doubao-seedance-1-0-pro-250528"
        ).strip()

    def base_url(self) -> str:
        return str(
            self.config.get("video_base_url")
            or self.config.get("ark_base_url")
            or "https://ark.cn-beijing.volces.com/api/v3"
        ).rstrip("/")

    def api_key(self) -> str:
        return str(
            self.config.get("video_api_key")
            or self.config.get("ark_api_key")
            or ""
        ).strip()

    def fallback_enabled(self) -> bool:
        return bool(self.config.get("zhipu_video_fallback_enabled", True))

    def fallback_model(self) -> str:
        return str(
            self.config.get("zhipu_video_model") or _ZHIPU_MODEL_DEFAULT
        ).strip()

    def fallback_base_url(self) -> str:
        return str(
            self.config.get("zhipu_video_base_url") or _ZHIPU_BASE_URL_DEFAULT
        ).rstrip("/")

    def fallback_api_key(self) -> str:
        return str(self.config.get("zhipu_video_api_key") or "").strip()

    def fallback_available(self) -> bool:
        return bool(
            self.fallback_enabled()
            and self.fallback_api_key()
            and self.fallback_model()
        )

    def models(self) -> list[str]:
        models = [self.model()]
        if self.fallback_enabled() and self.fallback_model():
            models.append(self.fallback_model())
        return models

    def preflight(self, prompt: str, ratio: str, duration: int, resolution: str) -> None:
        if not self.api_key() and not self.fallback_available():
            raise VideoConfigError(
                "缺少视频 API Key；请配置 video_api_key/ark_api_key，或配置 "
                "zhipu_video_api_key 启用 CogVideoX-Flash。"
            )
        if self.api_key() and not self.model():
            raise VideoConfigError("Seedance 视频模型不能为空。")
        if not str(prompt or "").strip():
            raise VideoConfigError("视频 prompt 不能为空。")
        if ratio not in SUPPORTED_RATIOS:
            raise VideoConfigError(
                "ratio 仅支持 adaptive、1:1、16:9、4:3、21:9、9:16、3:4。"
            )
        if duration not in SUPPORTED_DURATIONS:
            raise VideoConfigError("duration 当前仅支持 5 或 10 秒。")
        if resolution not in SUPPORTED_RESOLUTIONS:
            raise VideoConfigError("resolution 仅支持 480p、720p、1080p。")

    @staticmethod
    def _prompt_with_controls(
        prompt: str, ratio: str, duration: int, resolution: str
    ) -> str:
        text = str(prompt).strip()
        controls = [f"--ratio {ratio}", f"--dur {duration}"]
        if resolution:
            controls.append(f"--resolution {resolution}")
        return f"{text}  {'  '.join(controls)}"

    async def create_task(
        self,
        *,
        prompt: str,
        ratio: str,
        duration: int,
        resolution: str,
        first_frame_data_url: str | None,
        return_last_frame: bool,
    ) -> str:
        self.preflight(prompt, ratio, duration, resolution)

        if not self.api_key():
            return await self._create_zhipu_task(
                prompt=prompt,
                ratio=ratio,
                duration=duration,
                resolution=resolution,
                first_frame_data_url=first_frame_data_url,
            )

        try:
            return await self._create_ark_task(
                prompt=prompt,
                ratio=ratio,
                duration=duration,
                resolution=resolution,
                first_frame_data_url=first_frame_data_url,
                return_last_frame=return_last_frame,
            )
        except VideoApiError as exc:
            if not exc.fallback_allowed or not self.fallback_available():
                raise
            try:
                return await self._create_zhipu_task(
                    prompt=prompt,
                    ratio=ratio,
                    duration=duration,
                    resolution=resolution,
                    first_frame_data_url=first_frame_data_url,
                )
            except Exception as fallback_exc:
                raise VideoApiError(
                    "Seedance 不可用且 CogVideoX-Flash 轮换失败："
                    f"primary={str(exc)[:180]}；fallback={str(fallback_exc)[:180]}",
                    api_calls=exc.api_calls
                    + int(getattr(fallback_exc, "api_calls", 0) or 0),
                    fallback_allowed=False,
                ) from fallback_exc

    async def _create_ark_task(
        self,
        *,
        prompt: str,
        ratio: str,
        duration: int,
        resolution: str,
        first_frame_data_url: str | None,
        return_last_frame: bool,
    ) -> str:
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": self._prompt_with_controls(
                    prompt, ratio, duration, resolution
                ),
            }
        ]
        if first_frame_data_url:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": first_frame_data_url},
                }
            )
        payload = {
            "model": self.model(),
            "content": content,
            "return_last_frame": bool(return_last_frame),
        }
        data = await self._request_json(
            "POST",
            f"{self.base_url()}/contents/generations/tasks",
            json_body=payload,
            provider="ark",
        )
        task_id = str(data.get("id") or "").strip()
        if not task_id:
            raise VideoApiError("方舟未返回视频任务 ID。", api_calls=1)
        return task_id

    async def _create_zhipu_task(
        self,
        *,
        prompt: str,
        ratio: str,
        duration: int,
        resolution: str,
        first_frame_data_url: str | None,
    ) -> str:
        if not self.fallback_available():
            raise VideoConfigError("CogVideoX-Flash 轮换未配置 zhipu_video_api_key。")

        prompt_text = self._zhipu_prompt(prompt)
        image_url = None
        if first_frame_data_url:
            image_url = await self._prepare_zhipu_image(first_frame_data_url)

        size = self._zhipu_size(
            ratio=ratio,
            resolution=resolution,
            has_first_frame=bool(image_url),
        )
        payload: dict[str, Any] = {
            "model": self.fallback_model(),
            "prompt": prompt_text,
            "duration": int(duration),
            "quality": str(
                self.config.get("zhipu_video_quality") or "speed"
            ).strip().lower(),
            "fps": self._zhipu_fps(),
        }
        if image_url:
            payload["image_url"] = image_url
        if size:
            payload["size"] = size

        data = await self._request_json(
            "POST",
            f"{self.fallback_base_url()}/videos/generations",
            json_body=payload,
            provider="zhipu",
        )
        task_id = str(data.get("id") or "").strip()
        if not task_id:
            raise VideoApiError("智谱未返回视频任务 ID。", api_calls=1)
        return f"zhipu:{task_id}"

    @staticmethod
    def _zhipu_prompt(prompt: str) -> str:
        text = str(prompt or "").strip()
        return text if len(text) <= 512 else text[:512]

    def _zhipu_fps(self) -> int:
        try:
            fps = int(self.config.get("zhipu_video_fps") or 30)
        except (TypeError, ValueError):
            fps = 30
        return 60 if fps == 60 else 30

    @staticmethod
    def _zhipu_size(
        *, ratio: str, resolution: str, has_first_frame: bool
    ) -> str | None:
        if ratio == "adaptive":
            return None if has_first_frame else "1920x1080"
        mapping_1080 = {
            "16:9": "1920x1080",
            "9:16": "1080x1920",
            "1:1": "1024x1024",
            "21:9": "2048x1080",
        }
        size = mapping_1080.get(ratio)
        if size:
            if resolution in {"480p", "720p"}:
                return {
                    "16:9": "1280x720",
                    "9:16": "720x1280",
                    "1:1": "1024x1024",
                    "21:9": "2048x1080",
                }[ratio]
            return size
        if has_first_frame:
            return None
        raise VideoConfigError(
            f"CogVideoX-Flash 文生视频轮换暂不安全映射 ratio={ratio}；"
            "Seedance 原请求未被改写。"
        )

    async def _prepare_zhipu_image(self, data_url: str) -> str:
        header, sep, encoded = str(data_url or "").partition(",")
        if not sep or ";base64" not in header.lower():
            raise VideoConfigError("CogVideoX-Flash 首帧需要 Base64 Data URL。")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise VideoConfigError("CogVideoX-Flash 首帧 Base64 无效。") from exc

        mime = (
            header[5:].split(";", 1)[0].lower()
            if header.startswith("data:")
            else ""
        )
        if (
            mime in {"image/png", "image/jpeg", "image/jpg"}
            and len(raw) <= _ZHIPU_IMAGE_MAX_BYTES
        ):
            return data_url

        converted = await self._compress_first_frame(raw, mime)
        if len(converted) > _ZHIPU_IMAGE_MAX_BYTES:
            raise VideoConfigError("CogVideoX-Flash 首帧压缩后仍超过 5 MiB。")
        return "data:image/jpeg;base64," + base64.b64encode(converted).decode("ascii")

    async def _compress_first_frame(self, raw: bytes, mime: str) -> bytes:
        ffmpeg = str(self.config.get("ffmpeg_bin") or "ffmpeg").strip()
        suffix = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(mime, ".img")
        with tempfile.TemporaryDirectory(prefix="yangmo-cogvideo-") as tmp:
            root = Path(tmp)
            src = root / f"input{suffix}"
            dst = root / "first_frame.jpg"
            src.write_bytes(raw)
            process = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-i",
                os.fspath(src),
                "-frames:v",
                "1",
                "-vf",
                "scale=1920:1920:force_original_aspect_ratio=decrease",
                "-q:v",
                "5",
                os.fspath(dst),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise VideoConfigError("为 CogVideoX-Flash 压缩首帧超时。")
            if process.returncode != 0 or not dst.exists():
                detail = (stderr or b"").decode("utf-8", "ignore")[:240]
                raise VideoConfigError(
                    f"首帧超过智谱 5 MiB 限制且 ffmpeg 压缩失败：{detail}"
                )
            return dst.read_bytes()

    async def get_task(self, task_id: str) -> dict[str, Any]:
        if str(task_id).startswith("zhipu:"):
            raw_id = str(task_id).split(":", 1)[1]
            return await self._request_json(
                "GET",
                f"{self.fallback_base_url()}/async-result/{raw_id}",
                provider="zhipu",
            )
        return await self._request_json(
            "GET",
            f"{self.base_url()}/contents/generations/tasks/{task_id}",
            provider="ark",
        )

    async def wait_task(self, task_id: str) -> tuple[dict[str, Any], int]:
        if str(task_id).startswith("zhipu:"):
            return await self._wait_zhipu_task(task_id)
        return await self._wait_ark_task(task_id)

    async def _wait_ark_task(self, task_id: str) -> tuple[dict[str, Any], int]:
        interval = max(
            1.0,
            min(float(self.config.get("video_poll_interval_seconds") or 4), 30.0),
        )
        timeout = max(
            30.0,
            min(float(self.config.get("video_timeout_seconds") or 600), 3600.0),
        )
        deadline = asyncio.get_running_loop().time() + timeout
        api_calls = 1
        while True:
            if asyncio.get_running_loop().time() >= deadline:
                raise VideoApiError(
                    f"视频任务等待超时（task_id={task_id}）。任务可能仍在服务端运行，可稍后查询。",
                    api_calls=api_calls,
                    task_id=task_id,
                )
            await asyncio.sleep(interval)
            task = await self.get_task(task_id)
            api_calls += 1
            status = str(task.get("status") or "").lower()
            if status == "succeeded":
                return task, api_calls
            if status in {"failed", "cancelled"}:
                error = task.get("error") if isinstance(task.get("error"), dict) else {}
                code = str(error.get("code") or status)
                message = str(error.get("message") or "视频任务失败")
                raise VideoApiError(
                    f"视频任务失败：{code}: {message}",
                    api_calls=api_calls,
                    task_id=task_id,
                    fallback_allowed=False,
                )
            if status not in {"queued", "running", ""}:
                raise VideoApiError(
                    f"视频任务返回未知状态：{status}",
                    api_calls=api_calls,
                    task_id=task_id,
                )

    async def _wait_zhipu_task(self, task_id: str) -> tuple[dict[str, Any], int]:
        raw_id = str(task_id).split(":", 1)[1]
        interval = max(
            1.0,
            min(float(self.config.get("video_poll_interval_seconds") or 4), 30.0),
        )
        timeout = max(
            30.0,
            min(float(self.config.get("video_timeout_seconds") or 600), 3600.0),
        )
        deadline = asyncio.get_running_loop().time() + timeout
        api_calls = 1
        while True:
            if asyncio.get_running_loop().time() >= deadline:
                raise VideoApiError(
                    f"CogVideoX-Flash 任务等待超时（task_id={raw_id}）。",
                    api_calls=api_calls,
                    task_id=raw_id,
                )
            await asyncio.sleep(interval)
            data = await self.get_task(task_id)
            api_calls += 1
            status = str(
                data.get("task_status") or data.get("status") or ""
            ).upper()
            if status == "SUCCESS":
                result = data.get("video_result")
                rows = result if isinstance(result, list) else []
                first = rows[0] if rows and isinstance(rows[0], dict) else {}
                video_url = str(first.get("url") or "").strip()
                if not video_url:
                    raise VideoApiError(
                        "CogVideoX-Flash 成功但未返回 video_result.url。",
                        api_calls=api_calls,
                        task_id=raw_id,
                    )
                return (
                    {
                        "id": raw_id,
                        "status": "succeeded",
                        "model": str(data.get("model") or self.fallback_model()),
                        "content": {"video_url": video_url},
                        "provider": "zhipu",
                    },
                    api_calls,
                )
            if status in {"FAIL", "FAILED"}:
                error = data.get("error") if isinstance(data.get("error"), dict) else {}
                code = str(error.get("code") or "FAIL")
                message = str(
                    error.get("message") or "CogVideoX-Flash 视频任务失败"
                )
                raise VideoApiError(
                    f"CogVideoX-Flash 任务失败：{code}: {message}",
                    api_calls=api_calls,
                    task_id=raw_id,
                )
            if status not in {"PROCESSING", "PENDING", "RUNNING", ""}:
                raise VideoApiError(
                    f"CogVideoX-Flash 返回未知状态：{status}",
                    api_calls=api_calls,
                    task_id=raw_id,
                )

    @staticmethod
    def output_urls(task: dict[str, Any]) -> tuple[str, str | None]:
        content = task.get("content") if isinstance(task.get("content"), dict) else {}
        video_url = str(content.get("video_url") or "").strip()
        last_frame = None
        for key in ("last_frame_url", "last_frame_image_url", "last_frame"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                last_frame = value.strip()
                break
            if isinstance(value, dict):
                nested = str(value.get("url") or "").strip()
                if nested:
                    last_frame = nested
                    break
        return video_url, last_frame

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        if not str(url).startswith(("https://", "http://")):
            raise VideoApiError("视频结果 URL 不是 HTTP(S) 地址。", api_calls=0)
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status >= 400:
                    raise VideoApiError(
                        f"下载生成物失败：HTTP {response.status}", api_calls=0
                    )
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > max_bytes:
                    raise VideoApiError("生成视频超过插件下载大小上限。", api_calls=0)
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        raise VideoApiError(
                            "生成视频超过插件下载大小上限。", api_calls=0
                        )
                return bytes(chunks)

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        provider: str,
    ) -> dict[str, Any]:
        api_key = self.api_key() if provider == "ark" else self.fallback_api_key()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=60)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method, url, headers=headers, json=json_body
                ) as response:
                    text = await response.text()
                    try:
                        data = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        data = {}
                    if response.status >= 400:
                        error = data.get("error") if isinstance(data, dict) else None
                        if isinstance(error, dict):
                            code = str(error.get("code") or response.status)
                            message = str(error.get("message") or text[:300])
                        else:
                            code = str(response.status)
                            message = text[:300]
                        label = "方舟" if provider == "ark" else "智谱"
                        raise VideoApiError(
                            f"{label}视频 API 错误：{code}: {message}",
                            fallback_allowed=(
                                provider == "ark"
                                and _recoverable_provider_error(
                                    response.status, code, message
                                )
                            ),
                        )
                    if not isinstance(data, dict):
                        label = "方舟" if provider == "ark" else "智谱"
                        raise VideoApiError(f"{label}视频 API 返回格式异常。")
                    return data
        except asyncio.TimeoutError as exc:
            label = "方舟" if provider == "ark" else "智谱"
            raise VideoApiError(
                f"{label}视频 API 请求超时；结果状态未知，请避免立即重复提交。",
                fallback_allowed=False,
            ) from exc
        except aiohttp.ClientError as exc:
            label = "方舟" if provider == "ark" else "智谱"
            raise VideoApiError(
                f"{label}视频 API 网络错误：{str(exc)[:240]}",
                fallback_allowed=False,
            ) from exc
