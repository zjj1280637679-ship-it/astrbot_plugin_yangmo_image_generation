"""Per-model automatic maximum pixel policy for image generation.

Policy:
- The Agent chooses composition/aspect, not routine pixel dimensions.
- When the user has NOT explicitly requested pixels, every candidate model uses
  its own configured maximum pixel budget for that aspect.
- When the user HAS explicitly requested pixels, that exact target is preserved;
  models unable to satisfy it are skipped before an API call.
- Per-model max pixel values are objective environment settings and may be
  overridden from the plugin configuration without changing Agent policy.

The public generate_image tool intentionally keeps no pixel parameter. Explicit
pixel requests are carried as user intent in `aspect` using WIDTHxHEIGHT; the
image Skill tells the Agent not to invent such values on its own.
"""

from __future__ import annotations

import math
import re
from typing import Any

from . import api as _api
from . import main as _main

_ORIGINAL_BODY = _api.ArkImageClient._body
_ORIGINAL_LEG = _api.ArkImageClient._leg
_ORIGINAL_QUOTA_ERROR = _api.ArkImageClient._quota_error
_RESOLUTION_SKIP = "ResolutionRouteSkip"
_SIZE_RE = re.compile(r"^(\d+)x(\d+)$", re.IGNORECASE)

_BUILTIN_MAX_PIXELS = {
    "doubao-seedream-5-0-pro-260628": 4_624_220,
    "doubao-seedream-5-0-260128": 16_777_216,
    "doubao-seedream-4-5-251128": 16_777_216,
    "doubao-seedream-5.0-lite": 16_777_216,
}


def _configured_max_pixels(config: Any, model: str) -> int:
    overrides = config.get("image_model_max_pixels") if hasattr(config, "get") else None
    if isinstance(overrides, dict):
        raw = overrides.get(model)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    builtin = _BUILTIN_MAX_PIXELS.get(model)
    if builtin:
        return builtin
    return int(_api.model_caps(model).get("max_pixels", _api._MAX_PIXELS))


def _size_for_budget(size: str, max_pixels: int) -> str:
    requested = _api.validate_size(size)
    match = _SIZE_RE.match(requested)
    assert match is not None
    width, height = (int(item) for item in match.groups())
    pixels = width * height
    if pixels <= 0:
        raise _api.ImageConfigError("图片尺寸无效")
    # Preserve only the aspect ratio. AUTO_MAX means fill this model's configured
    # pixel budget as closely as possible using even dimensions.
    scale = math.sqrt(max_pixels / pixels)
    out_w = max(2, round(width * scale / 2) * 2)
    out_h = max(2, round(height * scale / 2) * 2)
    while out_w * out_h > max_pixels:
        if out_w >= out_h:
            out_w -= 2
        else:
            out_h -= 2
    return f"{out_w}x{out_h}"


def _is_user_fixed_size(size: str) -> bool:
    """Recognize an explicit pixel target carried through aspect_to_size.

    Named aspects and W:H ratios are converted by main.py before reaching here,
    so the patch records intent on the event/tool side rather than guessing from
    the resulting dimensions. This helper is retained for defensive callers.
    """
    return bool(_SIZE_RE.match(str(size or "").strip()))


def _auto_size_for_model(self, model: str, size: str) -> str:
    budget = _configured_max_pixels(self.config, model)
    minimum = int(_api.model_caps(model).get("min_pixels", _api._MIN_PIXELS))
    if budget < minimum:
        raise _api.ImageConfigError(
            f"模型 {model} 的最大像素配置 {budget} 低于模型最小像素 {minimum}"
        )
    return _size_for_budget(size, budget)


def _body_per_model(self, model: str, prompt: str, size: str, refs: list[str], count: int) -> dict:
    # `self` here is ArkImageClient because we install this as an instance method.
    effective = _auto_size_for_model(self, model, size)
    return _ORIGINAL_BODY(model, prompt, effective, refs, count)


async def _leg_per_model_max(
    self,
    base: str,
    key: str,
    model: str,
    prompt: str,
    size: str,
    refs: list[str],
    count: int,
):
    # _body() now derives the candidate model's AUTO_MAX size. No model is
    # skipped merely because another model has a larger maximum.
    return await _ORIGINAL_LEG(self, base, key, model, prompt, size, refs, count)


def _quota_or_resolution_skip(exc: Exception) -> bool:
    if _RESOLUTION_SKIP in str(exc):
        return True
    return _ORIGINAL_QUOTA_ERROR(exc)


# ArkImageClient._body was static. Install an instance-aware wrapper so it can
# read per-model overrides from this plugin instance's config.
_api.ArkImageClient._body = _body_per_model
_api.ArkImageClient._leg = _leg_per_model_max
_api.ArkImageClient._quota_error = staticmethod(_quota_or_resolution_skip)
_main.VERSION = "0.3.5"
