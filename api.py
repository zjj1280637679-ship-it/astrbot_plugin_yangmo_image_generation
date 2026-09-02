from __future__ import annotations

import asyncio
import io
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

import aiohttp


SEEDREAM_5_PRO_MODEL = "doubao-seedream-5-0-pro-260628"
SEEDREAM_5_PRO_MIN_PIXELS = 921_600
SEEDREAM_5_PRO_MAX_PIXELS = 4_624_220
SEEDREAM_5_PRO_MAX_REFS = 10
PRIMARY_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
PLAN_BASE_URL = "https://ark.cn-beijing.volces.com/api/plan/v3"

ASPECT_RATIOS: dict[str, tuple[int, int]] = {
    "landscape": (16, 9),
    "portrait": (9, 16),
    "square": (1, 1),
    "photo": (4, 3),
    "wide": (21, 9),
}
_RATIO_RE = re.compile(r"^(\d+):(\d+)$")
_SIZE_RE = re.compile(r"^(\d+)[xX×](\d+)$")
_MAX_ASPECT_RATIO = 16
_DOWNLOAD_CAP = 64 * 1024 * 1024


class ImageConfigError(ValueError):
    def __init__(self, message: str, *, api_calls: int = 0):
        super().__init__(message)
        self.api_calls = max(0, int(api_calls))


class ImageApiError(RuntimeError):
    def __init__(self, message: str, *, api_calls: int = 1):
        super().__init__(message)
        self.api_calls = max(0, int(api_calls))


@dataclass(frozen=True)
class SizeRequest:
    mode: str
    value: str
    ratio: tuple[int, int] | None = None


@dataclass(frozen=True)
class ImageModelCard:
    route: str
    label: str
    base_url: str
    api_key: str
    model: str
    max_pixels: int
    enabled: bool = True

    @property
    def is_plan(self) -> bool:
        return self.route == "plan_fallback"

    def public_dict(self) -> dict:
        return {
            "route": self.route,
            "label": self.label,
            "enabled": self.enabled,
            "base_url": self.base_url,
            "model": self.model,
            "max_pixels": self.max_pixels,
            "configured": bool(self.base_url and self.api_key and self.model),
        }


def _mapping(config: Mapping, key: str) -> Mapping:
    value = config.get(key, {})
    return value if isinstance(value, Mapping) else {}


def _text(value, default: str = "") -> str:
    result = str(value or "").strip()
    return result or default


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return parsed if parsed > 0 else default


def aspect_to_size(aspect: str, config: Mapping | None = None) -> SizeRequest:
    """Parse a tool-facing aspect into AUTO_MAX or USER_FIXED intent."""
    del config  # Kept in the signature for plugin compatibility.
    value = _text(aspect, "landscape").lower().replace(" ", "")
    if value in ASPECT_RATIOS:
        return SizeRequest("auto_max", value, ASPECT_RATIOS[value])
    ratio_match = _RATIO_RE.fullmatch(value)
    if ratio_match:
        width_ratio, height_ratio = (int(item) for item in ratio_match.groups())
        _validate_ratio(width_ratio, height_ratio)
        return SizeRequest("auto_max", value, (width_ratio, height_ratio))
    size_match = _SIZE_RE.fullmatch(value)
    if size_match:
        width, height = (int(item) for item in size_match.groups())
        _validate_dimensions(width, height, SEEDREAM_5_PRO_MAX_PIXELS)
        return SizeRequest("user_fixed", f"{width}x{height}")
    raise ImageConfigError(
        "aspect 必须是 landscape、portrait、square、photo、wide、W:H 或 WIDTHxHEIGHT"
    )


def _validate_ratio(width: int, height: int) -> None:
    ratio = width / height if height else 0
    if width <= 0 or height <= 0 or not 1 / _MAX_ASPECT_RATIO <= ratio <= _MAX_ASPECT_RATIO:
        raise ImageConfigError("图片宽高比必须在 1:16 到 16:1 之间")


def _validate_dimensions(width: int, height: int, max_pixels: int) -> None:
    _validate_ratio(width, height)
    pixels = width * height
    if pixels < SEEDREAM_5_PRO_MIN_PIXELS or pixels > max_pixels:
        raise ImageConfigError(
            f"Seedream 5 Pro 图片总像素必须在 {SEEDREAM_5_PRO_MIN_PIXELS} 到 {max_pixels} 之间"
        )


def _size_for_ratio(ratio: tuple[int, int], max_pixels: int) -> str:
    width_ratio, height_ratio = ratio
    _validate_ratio(width_ratio, height_ratio)
    if max_pixels < SEEDREAM_5_PRO_MIN_PIXELS:
        raise ImageConfigError(
            f"模型卡最大像素 {max_pixels} 低于 Seedream 5 Pro 最小像素 {SEEDREAM_5_PRO_MIN_PIXELS}"
        )
    max_pixels = min(max_pixels, SEEDREAM_5_PRO_MAX_PIXELS)
    scale = math.sqrt(max_pixels / (width_ratio * height_ratio))
    width = max(2, int(width_ratio * scale) // 2 * 2)
    height = max(2, int(height_ratio * scale) // 2 * 2)
    while width * height > max_pixels:
        if width >= height:
            width -= 2
        else:
            height -= 2
    _validate_dimensions(width, height, max_pixels)
    return f"{width}x{height}"


def _effective_size(card: ImageModelCard, request: SizeRequest) -> str:
    card_limit = min(card.max_pixels, SEEDREAM_5_PRO_MAX_PIXELS)
    if request.mode == "user_fixed":
        match = _SIZE_RE.fullmatch(request.value)
        if match is None:
            raise ImageConfigError("USER_FIXED 图片尺寸无效")
        width, height = (int(item) for item in match.groups())
        _validate_dimensions(width, height, card_limit)
        return f"{width}x{height}"
    if request.ratio is None:
        raise ImageConfigError("AUTO_MAX 缺少画幅比例")
    return _size_for_ratio(request.ratio, card_limit)


def image_dimensions(data: bytes, mime_type: str) -> tuple[int, int]:
    mime = str(mime_type).lower()
    if mime == "image/png":
        if (
            len(data) >= 24
            and data.startswith(b"\x89PNG\r\n\x1a\n")
            and data[12:16] == b"IHDR"
        ):
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        raise ImageConfigError("无法安全读取 PNG 参考图尺寸")
    if mime == "image/gif":
        if len(data) >= 10 and data.startswith((b"GIF87a", b"GIF89a")):
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        raise ImageConfigError("无法安全读取 GIF 参考图尺寸")
    if mime == "image/jpeg" and data.startswith(b"\xff\xd8"):
        position = 2
        sof_markers = {
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        }
        while position < len(data):
            if data[position] != 0xFF:
                position += 1
                continue
            while position < len(data) and data[position] == 0xFF:
                position += 1
            if position >= len(data):
                break
            marker = data[position]
            position += 1
            if marker == 0x00:
                continue
            if marker in sof_markers:
                if position + 7 > len(data):
                    break
                segment_length = int.from_bytes(data[position : position + 2], "big")
                if segment_length < 7 or position + segment_length > len(data):
                    break
                return (
                    int.from_bytes(data[position + 5 : position + 7], "big"),
                    int.from_bytes(data[position + 3 : position + 5], "big"),
                )
            if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if marker == 0xDA or position + 2 > len(data):
                break
            segment_length = int.from_bytes(data[position : position + 2], "big")
            if segment_length < 2 or position + segment_length > len(data):
                break
            position += segment_length
    if mime == "image/webp" and len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            return (
                int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF,
            )
    raise ImageConfigError("无法安全读取参考图尺寸")


def validate_reference_image(data: bytes, mime_type: str) -> tuple[int, int]:
    width, height = image_dimensions(data, mime_type)
    if width <= 14 or height <= 14:
        raise ImageConfigError("参考图宽和高都必须大于 14 像素")
    _validate_ratio(width, height)
    if width * height > 36_000_000:
        raise ImageConfigError("参考图总像素不能超过 3600 万")
    return width, height


def normalize_reference_format(data: bytes, mime_type: str) -> tuple[bytes, str]:
    """Convert WebP/GIF inputs to PNG because Seedream 5 Pro accepts JPEG/PNG refs."""
    mime = str(mime_type).lower()
    if mime in {"image/png", "image/jpeg"}:
        return data, mime
    if mime not in {"image/webp", "image/gif"}:
        raise ImageConfigError("参考图格式必须是 PNG、JPEG、WebP 或 GIF")
    try:
        from PIL import Image as PillowImage

        with PillowImage.open(io.BytesIO(data)) as image:
            image.seek(0)
            converted = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            output = io.BytesIO()
            converted.save(output, format="PNG", optimize=True)
    except Exception as exc:
        raise ImageConfigError("WebP/GIF 参考图转换为 PNG 失败") from exc
    return output.getvalue(), "image/png"


def sniff_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return "application/octet-stream"


def to_data_url(data: bytes, mime_type: str | None = None) -> str:
    import base64

    mime = mime_type or sniff_mime(data)
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class ArkImageClient:
    def __init__(self, config: Mapping):
        self.config = config

    @staticmethod
    def _bounded(value, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return min(max(parsed, minimum), maximum)

    def _http_timeout(self) -> int:
        return self._bounded(self.config.get("image_http_timeout_seconds"), 105, 10, 600)

    def _total_budget(self) -> int:
        return self._bounded(self.config.get("image_total_timeout_seconds"), 110, 20, 3600)

    def total_budget(self) -> int:
        """Public wall-clock budget shared by API generation and result download."""
        return self._total_budget()

    def model_cards(self) -> tuple[ImageModelCard, ImageModelCard]:
        primary_cfg = _mapping(self.config, "primary_model_card")
        primary = ImageModelCard(
            route="standard_primary",
            label="普通 API · Seedream 5 Pro",
            base_url=_text(primary_cfg.get("base_url"), _text(self.config.get("ark_base_url"), PRIMARY_BASE_URL)),
            api_key=_text(primary_cfg.get("api_key"), _text(self.config.get("ark_api_key"))),
            model=_text(primary_cfg.get("model"), SEEDREAM_5_PRO_MODEL),
            max_pixels=_positive_int(
                primary_cfg.get("max_pixels", self.config.get("image_max_pixels_seedream_5_pro")),
                SEEDREAM_5_PRO_MAX_PIXELS,
            ),
        )
        fallback_cfg = _mapping(self.config, "fallback_model_card")
        fallback = ImageModelCard(
            route="plan_fallback",
            label="Plan API 回退 · Seedream 5 Pro",
            base_url=_text(fallback_cfg.get("base_url"), _text(self.config.get("ark_plan_base_url"), PLAN_BASE_URL)),
            api_key=_text(fallback_cfg.get("api_key"), _text(self.config.get("ark_plan_api_key"), primary.api_key)),
            model=_text(fallback_cfg.get("model"), SEEDREAM_5_PRO_MODEL),
            max_pixels=_positive_int(fallback_cfg.get("max_pixels"), SEEDREAM_5_PRO_MAX_PIXELS),
            enabled=bool(fallback_cfg.get("enabled", self.config.get("ark_plan_fallback", True))),
        )
        return primary, fallback

    def models(self) -> list[str]:
        return [SEEDREAM_5_PRO_MODEL]

    def public_cards(self) -> list[dict]:
        return [card.public_dict() for card in self.model_cards()]

    def _candidate_cards(self, size: SizeRequest, refs: list[str]) -> list[tuple[ImageModelCard, str]]:
        if len(refs) > SEEDREAM_5_PRO_MAX_REFS:
            raise ImageConfigError(
                f"Seedream 5 Pro 最多接受 {SEEDREAM_5_PRO_MAX_REFS} 张参考图"
            )
        candidates: list[tuple[ImageModelCard, str]] = []
        rejected: list[str] = []
        for card in self.model_cards():
            if not card.enabled:
                continue
            if not card.base_url or not card.api_key or not card.model:
                rejected.append(f"{card.label} 未配置完整")
                continue
            if card.model != SEEDREAM_5_PRO_MODEL:
                rejected.append(f"{card.label} 模型必须是 {SEEDREAM_5_PRO_MODEL}")
                continue
            try:
                effective_size = _effective_size(card, size)
            except ImageConfigError as exc:
                rejected.append(f"{card.label}: {exc}")
                continue
            candidates.append((card, effective_size))
        if not candidates:
            detail = "；".join(rejected) or "两张模型卡均不可用"
            raise ImageConfigError(detail)
        return candidates

    def preflight(self, prompt: str, size: SizeRequest, refs: list[str]) -> None:
        if not _text(prompt):
            raise ImageConfigError("prompt 不能为空")
        for card, effective_size in self._candidate_cards(size, refs):
            self._body(card, prompt, effective_size, refs)

    @staticmethod
    def _fallback_safe(exc: Exception) -> bool:
        text = str(exc)
        return any(
            marker in text
            for marker in (
                "HTTP 429",
                "SetLimitExceeded",
                "LimitExceeded",
                "QuotaExceeded",
                "RateLimitExceeded",
                "TooManyRequests",
                "AccountOverdue",
                "InsufficientBalance",
                "ServerOverloaded",
            )
        )

    @staticmethod
    def _headers(key: str) -> dict:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async def _post(self, card: ImageModelCard, body: dict, timeout: float) -> dict:
        url = card.base_url.rstrip("/") + "/images/generations"
        try:
            async with aiohttp.ClientSession() as session, session.post(
                url,
                headers=self._headers(card.api_key),
                json=body,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                text = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise ImageApiError(f"HTTP {response.status}: {text[:600]}")
        except asyncio.TimeoutError as exc:
            raise ImageApiError("图片 API 请求超时") from exc
        except aiohttp.ClientError as exc:
            raise ImageApiError(f"图片 API 网络请求失败：{exc}") from exc
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ImageApiError("图片 API 返回无效 JSON") from exc
        if not isinstance(value, dict):
            raise ImageApiError("图片 API 返回形状无效")
        return value

    @staticmethod
    def _body(card: ImageModelCard, prompt: str, size: str, refs: list[str]) -> dict:
        body = {
            "model": card.model,
            "prompt": prompt,
            "size": size,
            "response_format": "url",
            "output_format": "png",
            "watermark": False,
        }
        if refs:
            body["image"] = refs
        return body

    async def _leg(
        self,
        card: ImageModelCard,
        prompt: str,
        size: str,
        refs: list[str],
        timeout: float,
    ) -> str:
        body = self._body(card, prompt, size, refs)
        try:
            value = await self._post(card, body, timeout)
        except ImageApiError:
            raise
        except aiohttp.ClientError as exc:
            raise ImageApiError(f"图片 API 网络请求失败：{exc}") from exc
        rows = value.get("data") or []
        if not isinstance(rows, list):
            raise ImageApiError("图片 API 返回 data 形状无效")
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = row.get("url")
            if isinstance(url, str) and url.strip():
                return url.strip()
        raise ImageApiError("图片 API 没有返回可下载图片")

    async def generate(
        self,
        prompt: str,
        size: SizeRequest,
        refs: list[str],
        *,
        deadline: float | None = None,
    ):
        candidates = self._candidate_cards(size, refs)
        loop = asyncio.get_running_loop()
        if deadline is None:
            deadline = loop.time() + self._total_budget()
        total_calls = 0
        last_error: ImageApiError | None = None
        for index, (card, effective_size) in enumerate(candidates):
            remaining = deadline - loop.time()
            if remaining <= 1:
                raise ImageApiError(
                    f"图片生成总时长预算（{self._total_budget()} 秒）已用尽",
                    api_calls=total_calls,
                )
            timeout = min(float(self._http_timeout()), remaining)
            try:
                url = await self._leg(card, prompt, effective_size, refs, timeout)
                return [url], total_calls + 1, card.model, card.is_plan, card.route, effective_size
            except ImageApiError as exc:
                total_calls += max(1, exc.api_calls)
                last_error = exc
                has_next = index + 1 < len(candidates)
                if not has_next or not self._fallback_safe(exc):
                    exc.api_calls = total_calls
                    raise
        if last_error is not None:
            last_error.api_calls = total_calls
            raise last_error
        raise ImageConfigError("没有可用的 Seedream 5 Pro 模型卡")

    async def download(self, url: str) -> bytes:
        try:
            async with aiohttp.ClientSession() as session, session.get(
                str(url), timeout=aiohttp.ClientTimeout(total=120)
            ) as response:
                if response.status < 200 or response.status >= 300:
                    raise ImageApiError(f"图片下载 HTTP {response.status}")
                declared = int(response.headers.get("Content-Length") or 0)
                if declared > _DOWNLOAD_CAP:
                    raise ImageApiError("图片超过 64 MiB 下载上限")
                chunks = []
                size = 0
                async for chunk in response.content.iter_chunked(256 * 1024):
                    size += len(chunk)
                    if size > _DOWNLOAD_CAP:
                        raise ImageApiError("图片超过 64 MiB 下载上限")
                    chunks.append(chunk)
                return b"".join(chunks)
        except asyncio.TimeoutError as exc:
            raise ImageApiError("图片下载超时") from exc
        except aiohttp.ClientError as exc:
            raise ImageApiError(f"图片下载网络错误：{exc}") from exc
