"""Keep image output resolution stable across model rotation.

The base client historically calls ``fit_size(model, requested_size)`` inside
``_body``. That silently shrinks the same requested 4K-ish size when the
selected model has a smaller pixel ceiling, while fallback models may keep the
full size. The result looks like random high/low resolution when the route
changes.

When ``image_preserve_requested_resolution`` is enabled (default), a model that
cannot honor the requested pixel dimensions is treated as an ineligible route
*before* making an API call. The client then continues to the next configured
image model. Explicitly disabling the option restores the old "prefer model,
allow silent downscale" behavior.
"""

from __future__ import annotations

from . import api as _api
from . import main as _main

_ORIGINAL_LEG = _api.ArkImageClient._leg
_ORIGINAL_QUOTA_ERROR = _api.ArkImageClient._quota_error
_RESOLUTION_SKIP = "ResolutionRouteSkip"


def _enabled(config) -> bool:
    value = config.get("image_preserve_requested_resolution", True)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"0", "false", "no", "off"}:
            return False
        if lowered in {"1", "true", "yes", "on"}:
            return True
    return True


async def _leg_preserve_resolution(
    self,
    base: str,
    key: str,
    model: str,
    prompt: str,
    size: str,
    refs: list[str],
    count: int,
):
    if _enabled(self.config):
        requested = _api.validate_size(size)
        effective = _api.fit_size(model, requested)
        if effective != requested:
            # Zero API calls: this route is skipped deterministically because
            # it cannot honor the user's/configured output dimensions.
            raise _api.ImageApiError(
                f"{_RESOLUTION_SKIP}: model={model} requested={requested} effective={effective}",
                api_calls=0,
            )
    return await _ORIGINAL_LEG(self, base, key, model, prompt, size, refs, count)


def _quota_or_resolution_skip(exc: Exception) -> bool:
    if _RESOLUTION_SKIP in str(exc):
        return True
    return _ORIGINAL_QUOTA_ERROR(exc)


_api.ArkImageClient._leg = _leg_preserve_resolution
_api.ArkImageClient._quota_error = staticmethod(_quota_or_resolution_skip)

# Imported after the other compatibility patches.
_main.VERSION = "0.3.4"
