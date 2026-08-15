"""Inject configurable defaults into generate_video tool calls.

The public tool remains Agent-controlled: any argument explicitly supplied by
an Agent/user wins. Only omitted arguments inherit plugin settings.

`first_frame` is deliberately not configurable globally. A global `current`
default would silently turn unrelated text-to-video requests into image-to-
video whenever an image happens to be present in context.
"""

from __future__ import annotations

from typing import Any

from astrbot.api.event import AstrMessageEvent, filter

from . import main as _main

_ALLOWED_DURATIONS = {5, 10}
_ALLOWED_RATIOS = {"adaptive", "1:1", "16:9", "4:3", "21:9", "9:16", "3:4"}
_ALLOWED_RESOLUTIONS = {"480p", "720p", "1080p"}


def _cfg_int(config: Any, key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (AttributeError, TypeError, ValueError):
        return default


def _cfg_str(config: Any, key: str, default: str) -> str:
    try:
        value = str(config.get(key, default) or "").strip().lower()
    except (AttributeError, TypeError):
        return default
    return value or default


def _cfg_bool(config: Any, key: str, default: bool) -> bool:
    try:
        value = config.get(key, default)
    except AttributeError:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value) if isinstance(value, int) else default


def _video_defaults(config: Any) -> dict[str, Any]:
    duration = _cfg_int(config, "video_default_duration", 10)
    if duration not in _ALLOWED_DURATIONS:
        duration = 10

    ratio = _cfg_str(config, "video_default_ratio", "adaptive")
    if ratio not in _ALLOWED_RATIOS:
        ratio = "adaptive"

    resolution = _cfg_str(config, "video_default_resolution", "1080p")
    if resolution not in _ALLOWED_RESOLUTIONS:
        resolution = "1080p"

    return {
        "duration": duration,
        "ratio": ratio,
        "resolution": resolution,
        "return_last_frame": _cfg_bool(
            config, "video_default_return_last_frame", True
        ),
        "auto_send": _cfg_bool(config, "video_default_auto_send", True),
        "announce": _cfg_bool(config, "video_default_announce", True),
    }


async def _apply_video_defaults(
    self,
    event: AstrMessageEvent,
    tool: Any,
    tool_args: dict | None,
) -> None:
    """Fill only omitted generate_video arguments from plugin settings."""
    del event
    if tool_args is None:
        return
    tool_name = str(getattr(tool, "name", "") or "")
    if tool_name != "generate_video":
        return
    for key, value in _video_defaults(self.config).items():
        tool_args.setdefault(key, value)


# Register this as a method of the existing Star. AstrBot registers handler
# ownership by module path, so present it as part of main.py before decorating.
_apply_video_defaults.__name__ = "apply_video_defaults"
_apply_video_defaults.__qualname__ = (
    "IndependentImageGeneration.apply_video_defaults"
)
_apply_video_defaults.__module__ = _main.__name__
setattr(
    _main.IndependentImageGeneration,
    "apply_video_defaults",
    _apply_video_defaults,
)
filter.on_using_llm_tool(priority=1000)(_apply_video_defaults)

# This patch is imported after quote_fastpath, which sets 0.3.2.
_main.VERSION = "0.3.3"
