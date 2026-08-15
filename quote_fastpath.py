"""Fast path for images inside quoted/replied QQ messages.

Design goals:
- Keep ``generate_image`` / ``generate_video`` platform-neutral at the tool layer.
- Treat ``current`` as "images attached to this event, including an explicitly
  quoted image when AstrBot can resolve it".
- Prefer zero-network data already embedded in ``Reply.chain``.
- Only if that is absent, reuse AstrBot's own quoted-message image extractor.
- Fail soft and fall back to the existing Agent resolver path instead of making
  a quote-resolution failure abort generation.

AstrBot 4.27.3 already ships ``extract_quoted_message_images``; using the core
helper avoids reimplementing OneBot/NapCat get_msg/get_image compatibility.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Image

try:
    from astrbot.core.message.components import Reply
except ImportError:  # pragma: no cover - compatibility only
    Reply = None  # type: ignore[assignment,misc]

try:
    from astrbot.core.utils.quoted_message_parser import extract_quoted_message_images
except ImportError:  # pragma: no cover - older AstrBot fallback
    try:
        from astrbot.core.utils.quoted_message.extractor import (
            extract_quoted_message_images,
        )
    except ImportError:
        extract_quoted_message_images = None  # type: ignore[assignment]

from . import main as _main

_ORIGINAL_CURRENT_IMAGES = _main.IndependentImageGeneration._current_images
_FASTPATH_MARKER = "_yangmo_quote_fastpath"


def _timeout_seconds(config: Any) -> float:
    try:
        value = float(config.get("quoted_image_fastpath_timeout_seconds", 3))
    except (AttributeError, TypeError, ValueError):
        value = 3.0
    return min(max(value, 0.5), 10.0)


def _top_level_chain(event) -> list[Any]:
    message_obj = getattr(event, "message_obj", None)
    chain = getattr(message_obj, "message", None)
    if hasattr(chain, "chain"):
        chain = chain.chain
    return chain if isinstance(chain, list) else []


def _reply_components(event) -> list[Any]:
    if Reply is None:
        return []
    return [item for item in _top_level_chain(event) if isinstance(item, Reply)]


def _embedded_reply_images(event) -> list[Image]:
    """Return images already embedded in Reply.chain without any OneBot call."""
    result: list[Image] = []
    for reply in _reply_components(event):
        chain = getattr(reply, "chain", None)
        if not isinstance(chain, list):
            continue
        for component in chain:
            if isinstance(component, Image):
                result.append(component)
    return result


async def _read_component(component: Image) -> tuple[bytes, str] | None:
    try:
        path = Path(await component.convert_to_file_path())
        if not path.exists() or not path.is_file():
            return None
        if path.stat().st_size > _main.MAX_REFERENCE_BYTES:
            logger.debug(
                "[yangmo.quote] skip quoted image over %s bytes: %s",
                _main.MAX_REFERENCE_BYTES,
                path,
            )
            return None
        data = await asyncio.to_thread(path.read_bytes)
        mime = _main.sniff_mime(data)
        if mime not in _main.SUPPORTED_IMAGE_MIME:
            return None
        # Validate here so malformed quote payloads cannot poison generation.
        _main.validate_reference_image(data, mime)
        return data, mime
    except Exception as exc:  # noqa: BLE001
        logger.debug("[yangmo.quote] embedded quoted image unavailable: %s", exc)
        return None


async def _read_resolved_ref(ref: str) -> tuple[bytes, str] | None:
    try:
        component = Image(file=str(ref))
        return await _read_component(component)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[yangmo.quote] resolved quoted ref unavailable: %s", exc)
        return None


async def _quoted_images(self, event) -> list[tuple[bytes, str]]:
    replies = _reply_components(event)
    if not replies:
        return []

    # Layer 1: zero/one-hop data AstrBot/NapCat already attached to Reply.chain.
    embedded = _embedded_reply_images(event)
    embedded_result: list[tuple[bytes, str]] = []
    for component in embedded[: _main.MAX_REFERENCE_IMAGES]:
        item = await _read_component(component)
        if item is not None:
            embedded_result.append(item)
    if embedded_result:
        event.set_extra(
            _FASTPATH_MARKER,
            {"route": "reply.chain", "images": len(embedded_result)},
        )
        return embedded_result

    # Layer 2: AstrBot's official quoted-message resolver. It may call OneBot
    # get_msg/get_image, so cap its latency. A miss must not block the Agent's
    # existing ctximg/resolved fallback path.
    if extract_quoted_message_images is None:
        return []

    try:
        refs = await asyncio.wait_for(
            extract_quoted_message_images(event, replies[0]),
            timeout=_timeout_seconds(self.config),
        )
    except asyncio.TimeoutError:
        logger.debug("[yangmo.quote] quoted image fast path timed out; use Agent fallback")
        event.set_extra(_FASTPATH_MARKER, {"route": "core", "status": "timeout"})
        return []
    except Exception as exc:  # noqa: BLE001
        logger.debug("[yangmo.quote] quoted image fast path failed: %s", exc)
        event.set_extra(_FASTPATH_MARKER, {"route": "core", "status": "failed"})
        return []

    result: list[tuple[bytes, str]] = []
    for ref in list(refs or [])[: _main.MAX_REFERENCE_IMAGES]:
        item = await _read_resolved_ref(str(ref))
        if item is not None:
            result.append(item)

    if result:
        event.set_extra(
            _FASTPATH_MARKER,
            {"route": "astrbot_quoted_message_parser", "images": len(result)},
        )
    return result


async def _current_images_with_quote(self, event) -> list[tuple[bytes, str]]:
    """Direct current images first, then quoted images as a fast-path extension.

    Ordering is intentional: an image freshly attached by the user remains the
    first ``current`` image for video first-frame selection. A quoted image is
    appended, not allowed to silently override an explicit current attachment.
    The lower-priority quoted extension is capped to the remaining reference
    budget so it cannot make a previously valid direct-image request fail just
    because the message also contains a Reply segment.
    """
    direct = await _ORIGINAL_CURRENT_IMAGES(self, event)
    remaining = max(0, _main.MAX_REFERENCE_IMAGES - len(direct))
    if remaining <= 0:
        return direct
    quoted = await _quoted_images(self, event)
    if not quoted:
        return direct
    return [*direct, *quoted[:remaining]]


# Patch the private media resolver only. Public tools, tool schemas, Agent
# ordering, delivery and external-plugin contracts remain unchanged.
_main.IndependentImageGeneration._current_images = _current_images_with_quote
_main.VERSION = "0.3.2"
