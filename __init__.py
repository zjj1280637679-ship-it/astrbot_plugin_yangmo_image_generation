"""Independent image/video generation plugin for AstrBot."""

# Keep provider ordering behind the existing provider-neutral video harness.
# CogVideoX-Flash is the preferred route when configured; Seedance 1.0 Pro
# remains the independent fallback route. Neither route requests audio.
from . import video_order_patch as _video_order_patch  # noqa: F401
