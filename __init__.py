"""Independent image/video generation plugin for AstrBot."""

# Keep provider-specific audio behavior behind the existing video harness.
# Importing this module patches ArkVideoClient before main.py imports it.
from . import audio_patch as _audio_patch  # noqa: F401
