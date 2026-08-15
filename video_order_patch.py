"""Provider ordering for the video harness.

CogVideoX-Flash is preferred whenever its API key is configured. Seedance 1.0
Pro remains the fallback/standalone route. This module deliberately adds no
audio prompt, audio model, or audio request parameter.
"""

from __future__ import annotations

from .video import ArkVideoClient, VideoApiError

_ORIGINAL_CREATE_TASK = ArkVideoClient.create_task
_ORIGINAL_REQUEST_JSON = ArkVideoClient._request_json


async def _request_json_with_symmetric_rotation(self, method, url, *, json_body=None, provider):
    """Preserve the client's request code while allowing explicit Zhipu create failures to rotate.

    The base client already marks recoverable Ark create failures. For Zhipu we
    only mark explicit HTTP/API failures as rotatable; timeout/network errors
    stay ambiguous and therefore must not trigger a duplicate submission.
    """
    try:
        return await _ORIGINAL_REQUEST_JSON(
            self, method, url, json_body=json_body, provider=provider
        )
    except VideoApiError as exc:
        if provider == "zhipu":
            text = str(exc).lower().replace("_", "").replace("-", "")
            recoverable = any(
                token in text
                for token in (
                    " 408", " 429", " 500", " 502", " 503", " 504",
                    "ratelimit", "toomanyrequests", "quota", "overload",
                    "serverbusy", "serviceunavailable", "capacity", "throttl",
                )
            )
            # Network/timeout errors explicitly carry unknown submission state.
            if "结果状态未知" not in str(exc) and recoverable:
                exc.fallback_allowed = True
        raise


async def _create_task_zhipu_first(
    self,
    *,
    prompt: str,
    ratio: str,
    duration: int,
    resolution: str,
    first_frame_data_url: str | None,
    return_last_frame: bool,
) -> str:
    self.preflight(prompt, ratio, duration, resolution)

    # Preferred route: CogVideoX-Flash when actually configured.
    if self.fallback_available():
        try:
            return await self._create_zhipu_task(
                prompt=prompt,
                ratio=ratio,
                duration=duration,
                resolution=resolution,
                first_frame_data_url=first_frame_data_url,
            )
        except VideoApiError as exc:
            # Do not duplicate an ambiguous submission. Rotate only after an
            # explicit recoverable refusal and only if Seedance is configured.
            if not exc.fallback_allowed or not self.api_key():
                raise
            try:
                return await self._create_ark_task(
                    prompt=prompt,
                    ratio=ratio,
                    duration=duration,
                    resolution=resolution,
                    first_frame_data_url=first_frame_data_url,
                    return_last_frame=return_last_frame,
                )
            except Exception as fallback_exc:
                raise VideoApiError(
                    "CogVideoX-Flash 不可用且 Seedance 1.0 Pro 轮换失败："
                    f"primary={str(exc)[:180]}；fallback={str(fallback_exc)[:180]}",
                    api_calls=exc.api_calls
                    + int(getattr(fallback_exc, "api_calls", 0) or 0),
                    fallback_allowed=False,
                ) from fallback_exc

    # No Zhipu key: keep Seedance independently usable.
    return await _ORIGINAL_CREATE_TASK(
        self,
        prompt=prompt,
        ratio=ratio,
        duration=duration,
        resolution=resolution,
        first_frame_data_url=first_frame_data_url,
        return_last_frame=return_last_frame,
    )


ArkVideoClient._request_json = _request_json_with_symmetric_rotation  # type: ignore[method-assign]
ArkVideoClient.create_task = _create_task_zhipu_first  # type: ignore[method-assign]
