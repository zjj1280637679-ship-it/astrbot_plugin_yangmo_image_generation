"""Seedream 5 Pro image generation plugin for AstrBot."""

# Extend the private `current` resolver with AstrBot's quoted-message fast path.
# Public tool names, parameters, ordering, and two-stage schema behavior stay native.
from . import quote_fastpath as _quote_fastpath  # noqa: F401
