from __future__ import annotations

import asyncio
import json
import math
import re

import aiohttp


class ImageConfigError(ValueError):
    def __init__(self, message: str, *, api_calls: int = 0):
        super().__init__(message)
        self.api_calls = max(0, int(api_calls))


class ImageApiError(RuntimeError):
    def __init__(self, message: str, *, api_calls: int = 1):
        super().__init__(message)
        self.api_calls = max(0, int(api_calls))


ASPECTS = {
    "landscape": "5461x3072",
    "portrait": "3072x5461",
    "square": "4096x4096",
    "photo": "4729x3547",
    "wide": "6240x2673",
}
MODEL_CAPS = {
    "doubao-seedream-4-5-251128": {
        "group": True,
        "max_refs": 14,
        "min_pixels": 3_686_400,
        "max_pixels": 16_777_216,
        "custom_output_format": False,
    },
    "doubao-seedream-5-0-260128": {
        "group": True,
        "max_refs": 14,
        "min_pixels": 3_686_400,
        "max_pixels": 16_777_216,
        "custom_output_format": True,
    },
    "doubao-seedream-5-0-pro-260628": {
        "group": False,
        "max_pixels": 4_624_220,
        "min_pixels": 921_600,
        "max_refs": 10,
        "custom_output_format": True,
    },
}
_RATIO_RE = re.compile(r"^(\d+):(\d+)$")
_SIZE_RE = re.compile(r"^(\d+)x(\d+)$", re.IGNORECASE)
_PIXEL_BUDGET = 4096 * 4096
_MIN_PIXELS = 921_600
_MAX_PIXELS = 4096 * 4096
_MAX_ASPECT_RATIO = 16
_MAX_OUTPUT_IMAGES = 15
_DOWNLOAD_CAP = 64 * 1024 * 1024


def require(config: dict, key: str):
    value = config.get(key) if isinstance(config, dict) else None
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ImageConfigError(f"缺少配置键 {key}")
    return value


def aspect_to_size(aspect: str, config: dict) -> str:
    value = str(aspect or "").strip()
    if value in ASPECTS:
        return ASPECTS[value]
    match = _RATIO_RE.match(value)
    if match:
        wr, hr = (int(item) for item in match.groups())
        ratio_value = wr / hr if hr else 0
        if wr > 0 and hr > 0 and 1 / _MAX_ASPECT_RATIO <= ratio_value <= _MAX_ASPECT_RATIO:
            scale = math.sqrt(_PIXEL_BUDGET / (wr * hr))
            width, height = wr * scale, hr * scale
            return validate_size(f"{max(2, int(width) // 2 * 2)}x{max(2, int(height) // 2 * 2)}")
        raise ImageConfigError("画幅比例必须在 1:16 到 16:1 之间")
    return validate_size(str(require(config, "image_size")))


def validate_size(size: str) -> str:
    match = _SIZE_RE.match(str(size or ""))
    if not match:
        raise ImageConfigError("图片尺寸必须是 WIDTHxHEIGHT")
    width, height = (int(item) for item in match.groups())
    pixels = width * height
    ratio = width / height if height else 0
    if pixels < _MIN_PIXELS or pixels > _MAX_PIXELS:
        raise ImageConfigError(
            f"图片总像素必须在 {_MIN_PIXELS} 到 {_MAX_PIXELS} 之间"
        )
    if not 1 / _MAX_ASPECT_RATIO <= ratio <= _MAX_ASPECT_RATIO:
        raise ImageConfigError("图片宽高比必须在 1:16 到 16:1 之间")
    return f"{width}x{height}"


def fit_size(model: str, size: str) -> str:
    size = validate_size(size)
    caps = model_caps(model)
    minimum = int(caps.get("min_pixels", _MIN_PIXELS))
    limit = int(caps.get("max_pixels", _MAX_PIXELS))
    match = _SIZE_RE.match(str(size or ""))
    if not match:
        raise ImageConfigError("图片尺寸必须是 WIDTHxHEIGHT")
    width, height = (int(item) for item in match.groups())
    pixels = width * height
    target = limit if pixels > limit else minimum if pixels < minimum else pixels
    if target != pixels:
        scale = math.sqrt(target / pixels)
        width = max(2, round(width * scale / 2) * 2)
        height = max(2, round(height * scale / 2) * 2)
    while width * height > limit:
        if width >= height:
            width -= 2
        else:
            height -= 2
    while width * height < minimum:
        if width >= height:
            width += 2
        else:
            height += 2
    return f"{width}x{height}"


def model_caps(model: str) -> dict:
    value = str(model or "").lower()
    if model in MODEL_CAPS:
        return dict(MODEL_CAPS[model])
    if "seedream" in value and "pro" in value and ("5-0" in value or "5.0" in value):
        return {
            "group": False,
            "min_pixels": 921_600,
            "max_pixels": 4_624_220,
            "max_refs": 10,
            "custom_output_format": True,
        }
    if "seedream" in value and ("5-0" in value or "5.0" in value):
        return {
            "group": True,
            "min_pixels": 3_686_400,
            "max_pixels": 16_777_216,
            "max_refs": 14,
            "custom_output_format": True,
        }
    if "seedream" in value and ("4-5" in value or "4.5" in value):
        return {
            "group": True,
            "min_pixels": 3_686_400,
            "max_pixels": 16_777_216,
            "max_refs": 14,
            "custom_output_format": False,
        }
    if "seedream" in value and ("4-0" in value or "4.0" in value):
        return {
            "group": True,
            "min_pixels": 921_600,
            "max_pixels": 16_777_216,
            "max_refs": 14,
            "custom_output_format": False,
        }
    return {
        "group": True,
        "min_pixels": _MIN_PIXELS,
        "max_pixels": _MAX_PIXELS,
        "max_refs": 14,
        "custom_output_format": True,
    }


def image_dimensions(data: bytes, mime_type: str) -> tuple[int, int]:
    mime = str(mime_type).lower()
    if mime == "image/png":
        if (
            len(data) >= 24
            and data.startswith(b"\x89PNG\r\n\x1a\n")
            and data[8:12] == b"\x00\x00\x00\r"
            and data[12:16] == b"IHDR"
        ):
            return (
                int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"),
            )
        raise ImageConfigError("无法安全读取 PNG 参考图尺寸")
    if mime == "image/gif":
        if len(data) >= 10 and data.startswith((b"GIF87a", b"GIF89a")):
            return (
                int.from_bytes(data[6:8], "little"),
                int.from_bytes(data[8:10], "little"),
            )
        raise ImageConfigError("无法安全读取 GIF 参考图尺寸")
    if mime == "image/jpeg" and data.startswith(b"\xff\xd8"):
        position = 2
        sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
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
            return (
                1 + int.from_bytes(data[24:27], "little"),
                1 + int.from_bytes(data[27:30], "little"),
            )
        if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            return (
                int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF,
            )
    raise ImageConfigError("无法安全读取参考图尺寸")


def validate_reference_image(data: bytes, mime_type: str) -> tuple[int, int]:
    width, height = image_dimensions(data, mime_type)
    if width <= 14 or height <= 14:
        raise ImageConfigError("参考图宽和高都必须大于 14 像素")
    ratio = width / height
    if not 1 / _MAX_ASPECT_RATIO <= ratio <= _MAX_ASPECT_RATIO:
        raise ImageConfigError("参考图宽高比必须在 1:16 到 16:1 之间")
    if width * height > 36_000_000:
        raise ImageConfigError("参考图总像素不能超过 3600 万")
    return width, height


def sniff_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
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
    def __init__(self, config: dict):
        self.config = config

    @staticmethod
    def _bounded(value, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return min(max(parsed, minimum), maximum)

    def _http_timeout(self) -> int:
        """Per-request HTTP timeout; configurable so slow backends can breathe."""
        return self._bounded(
            self.config.get("image_http_timeout_seconds"), 180, 30, 600
        )

    def _total_budget(self) -> int:
        """Wall-clock budget for the whole generate() call (all model legs)."""
        return self._bounded(
            self.config.get("image_total_timeout_seconds"), 280, 60, 3600
        )

    def models(self) -> list[str]:
        result = []
        for model in self.config.get("image_models") or []:
            value = str(model or "").strip()
            if value and value not in result:
                result.append(value)
        return result or [str(require(self.config, "image_model"))]

    def _standard_context(
        self, refs: list[str], count: int
    ) -> tuple[str, str, list[str]]:
        if count < 1 or count > _MAX_OUTPUT_IMAGES:
            raise ImageConfigError(f"count 必须在 1 到 {_MAX_OUTPUT_IMAGES} 之间")
        base = str(require(self.config, "ark_base_url"))
        key = str(require(self.config, "ark_api_key"))
        compatible = []
        for model in self.models():
            caps = model_caps(model)
            if count > 1 and not caps.get("group", True):
                continue
            if len(refs) > int(caps.get("max_refs", 14)):
                continue
            compatible.append(model)
        if not compatible:
            raise ImageConfigError("没有与当前张数和参考图数量兼容的普通图片模型")
        return base, key, compatible

    def preflight(self, prompt: str, size: str, refs: list[str], count: int) -> None:
        """Validate every deterministic, zero-request standard-path condition."""
        if not str(prompt or "").strip():
            raise ImageConfigError("prompt 不能为空")
        _, _, compatible = self._standard_context(refs, count)
        for model in compatible:
            self._body(model, prompt, size, refs, count)

    @staticmethod
    def _quota_error(exc: Exception) -> bool:
        text = str(exc)
        return "HTTP 429" in text or any(
            value in text
            for value in (
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

    async def _post(self, base: str, key: str, body: dict, timeout: int | None = None) -> dict:
        if timeout is None:
            timeout = self._http_timeout()
        url = str(base).rstrip("/") + "/images/generations"
        try:
            async with aiohttp.ClientSession() as session, session.post(
                url,
                headers=self._headers(key),
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
    def _body(model: str, prompt: str, size: str, refs: list[str], count: int) -> dict:
        body = {
            "model": model,
            "prompt": prompt,
            "size": fit_size(model, size),
            "response_format": "url",
            "watermark": False,
        }
        if model_caps(model).get("custom_output_format", True):
            body["output_format"] = "png"
        if refs:
            body["image"] = refs
        if count > 1 and model_caps(model).get("group", True):
            body["sequential_image_generation"] = "auto"
            body["sequential_image_generation_options"] = {"max_images": count}
        return body

    async def _leg(self, base: str, key: str, model: str, prompt: str, size: str, refs: list[str], count: int, timeout: int | None = None):
        body = self._body(model, prompt, size, refs, count)
        calls = 1
        try:
            value = await self._post(base, key, body, timeout=timeout)
        except ImageApiError as exc:
            exc.api_calls = calls
            raise
        except aiohttp.ClientError as exc:
            raise ImageApiError(
                f"图片 API 网络请求失败：{exc}", api_calls=calls
            ) from exc
        rows = value.get("data")
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise ImageApiError("图片 API 返回 data 形状无效", api_calls=calls)
        urls = []
        for row in rows:
            if not isinstance(row, dict):
                raise ImageApiError("图片 API 返回图片项形状无效", api_calls=calls)
            url = row.get("url")
            if url is None:
                continue
            if not isinstance(url, str) or not url.strip():
                raise ImageApiError("图片 API 返回图片 URL 形状无效", api_calls=calls)
            urls.append(url)
        if not urls:
            raise ImageApiError("图片 API 没有返回可下载图片", api_calls=calls)
        return urls[:count], calls

    async def generate(self, prompt: str, size: str, refs: list[str], count: int):
        base, key, compatible = self._standard_context(refs, count)
        total_calls = 0
        last_error = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._total_budget()
        for model in compatible:
            remaining = deadline - loop.time()
            if remaining <= 5:
                raise ImageApiError(
                    f"图片生成总时长预算（{self._total_budget()} 秒）已用尽，"
                    "未再尝试下一个模型；请稍后重试或调高 image_total_timeout_seconds。",
                    api_calls=total_calls,
                )
            leg_timeout = min(self._http_timeout(), max(30, int(remaining) - 5))
            try:
                urls, calls = await self._leg(
                    base, key, model, prompt, size, refs, count, timeout=leg_timeout
                )
                return urls, total_calls + calls, model, False
            except ImageApiError as exc:
                total_calls += exc.api_calls
                if not self._quota_error(exc):
                    exc.api_calls = total_calls
                    raise
                last_error = exc
        if not bool(self.config.get("ark_plan_fallback", True)):
            if last_error is not None:
                last_error.api_calls = total_calls
                raise last_error
            raise ImageConfigError("没有与当前张数和参考图数量兼容的普通图片模型")
        if last_error is None:
            raise ImageConfigError("没有与当前张数和参考图数量兼容的普通图片模型")
        plan_base = str(require(self.config, "ark_plan_base_url"))
        plan_key = str(self.config.get("ark_plan_api_key") or key)
        plan_model = str(require(self.config, "ark_plan_image_model"))
        plan_caps = model_caps(plan_model)
        if count > 1 and not plan_caps.get("group", True):
            raise ImageConfigError(
                "套餐图片模型不支持一次生成多张图",
                api_calls=total_calls,
            )
        if len(refs) > int(plan_caps.get("max_refs", 14)):
            raise ImageConfigError(
                "参考图数量超过套餐图片模型上限",
                api_calls=total_calls,
            )
        remaining = deadline - loop.time()
        if remaining <= 5:
            raise ImageApiError(
                f"图片生成总时长预算（{self._total_budget()} 秒）已用尽，"
                "未再尝试套餐兜底模型。",
                api_calls=total_calls,
            )
        leg_timeout = min(self._http_timeout(), max(30, int(remaining) - 5))
        try:
            urls, calls = await self._leg(
                plan_base, plan_key, plan_model, prompt, size, refs, count,
                timeout=leg_timeout,
            )
        except ImageApiError as exc:
            exc.api_calls += total_calls
            raise
        return urls, total_calls + calls, plan_model, True

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
