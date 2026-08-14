from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp


class VideoConfigError(ValueError):
    pass


class VideoApiError(RuntimeError):
    def __init__(self, message: str, *, api_calls: int = 1, task_id: str = ""):
        super().__init__(message)
        self.api_calls = max(0, int(api_calls))
        self.task_id = str(task_id or "")


SUPPORTED_RATIOS = {"adaptive", "1:1", "16:9", "4:3", "21:9", "9:16", "3:4"}
SUPPORTED_DURATIONS = {5, 10}
SUPPORTED_RESOLUTIONS = {"480p", "720p", "1080p"}


class ArkVideoClient:
    def __init__(self, config: dict):
        self.config = config

    def model(self) -> str:
        return str(self.config.get("video_model") or "doubao-seedance-1-0-pro-250528").strip()

    def base_url(self) -> str:
        return str(self.config.get("video_base_url") or self.config.get("ark_base_url") or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")

    def api_key(self) -> str:
        return str(self.config.get("video_api_key") or self.config.get("ark_api_key") or "").strip()

    def preflight(self, prompt: str, ratio: str, duration: int, resolution: str) -> None:
        if not self.api_key():
            raise VideoConfigError("缺少视频 API Key；请配置 video_api_key，或复用 ark_api_key。")
        if not self.model():
            raise VideoConfigError("视频模型不能为空。")
        if not str(prompt or "").strip():
            raise VideoConfigError("视频 prompt 不能为空。")
        if ratio not in SUPPORTED_RATIOS:
            raise VideoConfigError("ratio 仅支持 adaptive、1:1、16:9、4:3、21:9、9:16、3:4。")
        if duration not in SUPPORTED_DURATIONS:
            raise VideoConfigError("duration 当前仅支持 5 或 10 秒。")
        if resolution not in SUPPORTED_RESOLUTIONS:
            raise VideoConfigError("resolution 仅支持 480p、720p、1080p。")

    @staticmethod
    def _prompt_with_controls(prompt: str, ratio: str, duration: int, resolution: str) -> str:
        # Seedance 1.0's Ark API accepts generation controls in the text payload.
        # Keep controls at the end so user/agent prose remains clean and auditable.
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
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": self._prompt_with_controls(prompt, ratio, duration, resolution),
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
        data = await self._request_json("POST", f"{self.base_url()}/contents/generations/tasks", json_body=payload)
        task_id = str(data.get("id") or "").strip()
        if not task_id:
            raise VideoApiError("方舟未返回视频任务 ID。", api_calls=1)
        return task_id

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._request_json("GET", f"{self.base_url()}/contents/generations/tasks/{task_id}")

    async def wait_task(self, task_id: str) -> tuple[dict[str, Any], int]:
        interval = max(1.0, min(float(self.config.get("video_poll_interval_seconds") or 4), 30.0))
        timeout = max(30.0, min(float(self.config.get("video_timeout_seconds") or 600), 3600.0))
        deadline = asyncio.get_running_loop().time() + timeout
        api_calls = 1  # create_task
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
                )
            if status not in {"queued", "running", ""}:
                raise VideoApiError(
                    f"视频任务返回未知状态：{status}",
                    api_calls=api_calls,
                    task_id=task_id,
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
                    raise VideoApiError(f"下载生成物失败：HTTP {response.status}", api_calls=0)
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > max_bytes:
                    raise VideoApiError("生成视频超过插件下载大小上限。", api_calls=0)
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        raise VideoApiError("生成视频超过插件下载大小上限。", api_calls=0)
                return bytes(chunks)

    async def _request_json(self, method: str, url: str, *, json_body: dict | None = None) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key()}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=60)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=headers, json=json_body) as response:
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
                        raise VideoApiError(f"方舟视频 API 错误：{code}: {message}")
                    if not isinstance(data, dict):
                        raise VideoApiError("方舟视频 API 返回格式异常。")
                    return data
        except asyncio.TimeoutError as exc:
            raise VideoApiError("方舟视频 API 请求超时；结果状态未知，请避免立即重复付费提交。") from exc
        except aiohttp.ClientError as exc:
            raise VideoApiError(f"方舟视频 API 网络错误：{str(exc)[:240]}") from exc
