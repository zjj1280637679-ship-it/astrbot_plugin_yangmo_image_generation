"""Independent image/video generation plugin for AstrBot."""

# Keep provider ordering behind the existing provider-neutral video harness.
# CogVideoX-Flash is the preferred route when configured; Seedance 1.0 Pro
# remains the independent fallback route. Neither route requests audio.
from . import video_order_patch as _video_order_patch  # noqa: F401

# Extend only the private `current` image resolver: direct attachments remain
# first, while an explicitly quoted/replied image gets a low-latency fast path
# before the Agent needs to call an external ctximg/resolver tool.
from . import quote_fastpath as _quote_fastpath  # noqa: F401

# Fill omitted generate_video arguments from plugin settings. Explicit Agent
# arguments always win; this changes defaults, not permissions or tool order.
from . import video_defaults_patch as _video_defaults_patch  # noqa: F401

# Keep requested image dimensions stable across model rotation. Models that
# cannot honor the requested dimensions are skipped without an API call unless
# the user explicitly disables this policy in plugin settings.
from . import image_resolution_patch as _image_resolution_patch  # noqa: F401
