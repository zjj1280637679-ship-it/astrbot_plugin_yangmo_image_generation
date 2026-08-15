"""Audio defaults for the video harness.

This module keeps video generation provider-neutral at the AstrBot tool layer:
- Seedance uses an audio-capable Seedance model when sound is enabled.
- CogVideoX-Flash sends ``with_audio=true``.
- Prompts get a lightweight sound-design clause when the Agent did not already
  describe sound explicitly.

Imported from package ``__init__`` so the existing ``ArkVideoClient`` public
interface does not need another provider-specific tool or a workflow gate.
"""

from __future__ import annotations

from typing import Any

from .video import ArkVideoClient, VideoApiError


_DEFAULT_AUDIO_MODEL = "doubao-seedance-1-5-pro-251215"
_AUDIO_TERMS = (
    "声音",
    "音效",
    "环境声",
    "对白",
    "旁白",
    "配乐",
    "音乐",
    "人声",
    "口播",
    "bgm",
    "audio",
    "sound",
    "voice",
    "music",
)
_DEFAULT_SOUND_DESCRIPTION = (
    "声音：生成与画面内容和动作严格同步的自然环境声、动作音效与空间氛围；"
    "若画面存在明确说话行为，则生成与人物口型和情绪匹配的自然人声；"
    "没有明确说话行为时不要凭空添加旁白，配乐不要盖过主体声音。"
)

_ORIGINAL_MODEL = ArkVideoClient.model
_ORIGINAL_MODELS = ArkVideoClient.models


def _audio_enabled(self: ArkVideoClient) -> bool:
    return bool(self.config.get("video_with_audio", True))


def _supports_native_audio(model: str) -> bool:
    value = str(model or "").lower()
    return "seedance-1-5-pro" in value or "seedance-2" in value


def _audio_model(self: ArkVideoClient) -> str:
    return str(
        self.config.get("video_audio_model") or _DEFAULT_AUDIO_MODEL
    ).strip()


def _effective_model(self: ArkVideoClient) -> str:
    configured = _ORIGINAL_MODEL(self)
    if not _audio_enabled(self):
        return configured
    if _supports_native_audio(configured):
        return configured
    return _audio_model(self)


def _ensure_sound_description(self: ArkVideoClient, prompt: str) -> str:
    text = str(prompt or "").strip()
    if not _audio_enabled(self):
        return text
    lower = text.lower()
    if any(term in lower for term in _AUDIO_TERMS):
        return text
    return f"{text}\n{_DEFAULT_SOUND_DESCRIPTION}" if text else _DEFAULT_SOUND_DESCRIPTION


def _zhipu_prompt_preserve_audio(self: ArkVideoClient, prompt: str) -> str:
    """Keep CogVideoX's 512-char bound without dropping sound instructions."""
    text = _ensure_sound_description(self, prompt)
    if len(text) <= 512:
        return text

    marker = "\n声音："
    if marker in text:
        visual, sound = text.rsplit(marker, 1)
        sound = "声音：" + sound
        reserve = min(len(sound), 180)
        visual_budget = max(1, 512 - reserve - 1)
        return visual[:visual_budget].rstrip() + "\n" + sound[:reserve]
    return text[:512]


async def _create_ark_task_audio(
    self: ArkVideoClient,
    *,
    prompt: str,
    ratio: str,
    duration: int,
    resolution: str,
    first_frame_data_url: str | None,
    return_last_frame: bool,
) -> str:
    model = _effective_model(self)
    prompt_text = _ensure_sound_description(self, prompt)
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": self._prompt_with_controls(
                prompt_text, ratio, duration, resolution
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

    payload: dict[str, Any] = {
        "model": model,
        "content": content,
        "return_last_frame": bool(return_last_frame),
    }
    if _audio_enabled(self) and _supports_native_audio(model):
        payload["generate_audio"] = True

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


async def _create_zhipu_task_audio(
    self: ArkVideoClient,
    *,
    prompt: str,
    ratio: str,
    duration: int,
    resolution: str,
    first_frame_data_url: str | None,
) -> str:
    if not self.fallback_available():
        from .video import VideoConfigError

        raise VideoConfigError("CogVideoX-Flash 轮换未配置 zhipu_video_api_key。")

    prompt_text = _zhipu_prompt_preserve_audio(self, prompt)
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
        "with_audio": _audio_enabled(self),
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


def _models_with_audio(self: ArkVideoClient) -> list[str]:
    models: list[str] = []
    for model in (_effective_model(self), _ORIGINAL_MODEL(self)):
        if model and model not in models:
            models.append(model)
    if self.fallback_enabled() and self.fallback_model():
        if self.fallback_model() not in models:
            models.append(self.fallback_model())
    return models


# Patch the existing client class rather than introducing another AstrBot tool.
ArkVideoClient.model = _effective_model  # type: ignore[method-assign]
ArkVideoClient.models = _models_with_audio  # type: ignore[method-assign]
ArkVideoClient._create_ark_task = _create_ark_task_audio  # type: ignore[method-assign]
ArkVideoClient._create_zhipu_task = _create_zhipu_task_audio  # type: ignore[method-assign]
ArkVideoClient._zhipu_prompt = _zhipu_prompt_preserve_audio  # type: ignore[method-assign]
