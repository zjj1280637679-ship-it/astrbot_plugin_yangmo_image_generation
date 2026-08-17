"""Per-model automatic maximum pixel policy for image generation.

Policy:
- Named aspects / W:H ratios are AUTO_MAX: each candidate model uses its own
  configured maximum pixel budget while preserving the requested composition.
- WIDTHxHEIGHT is USER_FIXED: it is accepted only as an explicit pixel target
  and must be preserved exactly. A model that cannot satisfy it is skipped with
  zero API calls; rotation may continue to a compatible model.
- The image Skill tells the Agent never to invent WIDTHxHEIGHT merely for speed
  or quality. Only an explicit user pixel/resolution request should use it.

AstrBot 4.27.x expects flat plugin configuration entries here, so built-in
models use separate integer fields instead of an object/map setting.
"""

from __future__ import annotations

import math
import re
from typing import Any

from . import api as _api
from . import main as _main

_ORIGINAL_ASPECT_TO_SIZE = _main.aspect_to_size
_ORIGINAL_BODY = _api.ArkImageClient._body
_ORIGINAL_QUOTA_ERROR = _api.ArkImageClient._quota_error
_RESOLUTION_SKIP = "ResolutionRouteSkip"
_SIZE_RE = re.compile(r"^(\d+)x(\d+)$", re.IGNORECASE)
_FIXED_PREFIX = "user-fixed:"

_MODEL_PIXEL_FIELDS = {
    "doubao-seedream-5-0-pro-260628": ("image_max_pixels_seedream_5_pro", 4_624_220),
    "doubao-seedream-5-0-260128": ("image_max_pixels_seedream_5", 16_777_216),
    "doubao-seedream-4-5-251128": ("image_max_pixels_seedream_4_5", 16_777_216),
    "doubao-seedream-5.0-lite": ("image_max_pixels_seedream_lite", 16_777_216),
}


def _configured_max_pixels(config: Any, model: str) -> int:
    field, builtin = _MODEL_PIXEL_FIELDS.get(
        model,
        ("", int(_api.model_caps(model).get("max_pixels", _api._MAX_PIXELS))),
    )
    if not field:
        return builtin
    try:
        value = int(config.get(field, builtin))
    except (AttributeError, TypeError, ValueError):
        value = builtin
    return value if value > 0 else builtin


def _size_for_budget(size: str, max_pixels: int) -> str:
    requested = _api.validate_size(size)
    match = _SIZE_RE.match(requested)
    assert match is not None
    width, height = (int(item) for item in match.groups())
    scale = math.sqrt(max_pixels / (width * height))
    out_w = max(2, round(width * scale / 2) * 2)
    out_h = max(2, round(height * scale / 2) * 2)
    while out_w * out_h > max_pixels:
        if out_w >= out_h:
            out_w -= 2
        else:
            out_h -= 2
    return f"{out_w}x{out_h}"


def _aspect_to_size_with_explicit_pixels(aspect: str, config: dict) -> str:
    value = str(aspect or "").strip().lower()
    if _SIZE_RE.match(value):
        exact = _api.validate_size(value)
        return _FIXED_PREFIX + exact
    return _ORIGINAL_ASPECT_TO_SIZE(aspect, config)


def _effective_size(self, model: str, size: str) -> str:
    text = str(size or "")
    budget = _configured_max_pixels(self.config, model)
    caps = _api.model_caps(model)
    minimum = int(caps.get("min_pixels", _api._MIN_PIXELS))
    if budget < minimum:
        raise _api.ImageConfigError(
            f"模型 {model} 的最大像素配置 {budget} 低于模型最小像素 {minimum}"
        )

    if text.startswith(_FIXED_PREFIX):
        exact = _api.validate_size(text[len(_FIXED_PREFIX):])
        match = _SIZE_RE.match(exact)
        assert match is not None
        width, height = (int(item) for item in match.groups())
        pixels = width * height
        if pixels > budget or pixels < minimum:
            raise _api.ImageApiError(
                f"{_RESOLUTION_SKIP}: model={model} user_fixed={exact} "
                f"allowed_pixels={minimum}..{budget}",
                api_calls=0,
            )
        return exact

    return _size_for_budget(text, budget)


def _body_per_model(self, model: str, prompt: str, size: str, refs: list[str], count: int) -> dict:
    effective = _effective_size(self, model, size)
    return _ORIGINAL_BODY(model, prompt, effective, refs, count)


def _preflight_per_model(self, prompt: str, size: str, refs: list[str], count: int) -> None:
    if not str(prompt or "").strip():
        raise _api.ImageConfigError("prompt 不能为空")
    _, _, compatible = self._standard_context(refs, count)
    usable = 0
    last_skip: Exception | None = None
    for model in compatible:
        try:
            self._body(model, prompt, size, refs, count)
            usable += 1
        except _api.ImageApiError as exc:
            if _RESOLUTION_SKIP in str(exc):
                last_skip = exc
                continue
            raise
    if usable == 0:
        if last_skip is not None:
            raise _api.ImageConfigError(
                "用户指定的像素尺寸没有任何当前普通图片模型能够原样满足"
            )
        raise _api.ImageConfigError("没有与当前请求兼容的普通图片模型")


def _quota_or_resolution_skip(exc: Exception) -> bool:
    if _RESOLUTION_SKIP in str(exc):
        return True
    return _ORIGINAL_QUOTA_ERROR(exc)


_main.aspect_to_size = _aspect_to_size_with_explicit_pixels
_api.ArkImageClient._body = _body_per_model
_api.ArkImageClient.preflight = _preflight_per_model
_api.ArkImageClient._quota_error = staticmethod(_quota_or_resolution_skip)
_main.VERSION = "0.3.8"
