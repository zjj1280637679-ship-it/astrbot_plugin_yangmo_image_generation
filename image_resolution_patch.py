"""Keep image output resolution stable and maximal by default.

Policy:
- Pixel dimensions are a harness concern, not something the Agent should vary on
  its own. Unless the user explicitly asks for a different pixel size, image
  generation uses the maximum target size derived from the requested aspect.
- Model rotation must never silently downscale that target. A route that cannot
  honor the target dimensions is skipped before any API call, and the client
  continues to the next configured image model.

This module therefore makes resolution preservation unconditional. There is no
setting that can accidentally re-enable silent downscaling.
"""

from __future__ import annotations

from . import api as _api
from . import main as _main

_ORIGINAL_LEG = _api.ArkImageClient._leg
_ORIGINAL_QUOTA_ERROR = _api.ArkImageClient._quota_error
_RESOLUTION_SKIP = "ResolutionRouteSkip"


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
    requested = _api.validate_size(size)
    effective = _api.fit_size(model, requested)
    if effective != requested:
        # Zero API calls: this route is deterministically incompatible with the
        # requested/default pixel target. Never silently shrink it.
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
_main.VERSION = "0.3.5"
