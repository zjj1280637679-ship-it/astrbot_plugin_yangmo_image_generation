from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientError = type("ClientError", (Exception,), {})
    aiohttp_stub.ClientSession = object
    aiohttp_stub.ClientTimeout = lambda **kwargs: kwargs
    sys.modules["aiohttp"] = aiohttp_stub

SPEC = importlib.util.spec_from_file_location("yangmo_image_api", ROOT / "api.py")
assert SPEC is not None and SPEC.loader is not None
api = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = api
SPEC.loader.exec_module(api)


def config(*, primary_max: int = api.SEEDREAM_5_PRO_MAX_PIXELS) -> dict:
    return {
        "primary_model_card": {
            "model": api.SEEDREAM_5_PRO_MODEL,
            "base_url": api.PRIMARY_BASE_URL,
            "api_key": "primary-key",
            "max_pixels": primary_max,
        },
        "fallback_model_card": {
            "enabled": True,
            "model": api.SEEDREAM_5_PRO_MODEL,
            "base_url": api.PLAN_BASE_URL,
            "api_key": "plan-key",
            "max_pixels": api.SEEDREAM_5_PRO_MAX_PIXELS,
        },
        "image_http_timeout_seconds": 105,
        "image_total_timeout_seconds": 110,
    }


class ApiTests(unittest.TestCase):
    def test_exactly_two_seedream_5_pro_cards(self) -> None:
        primary, fallback = api.ArkImageClient(config()).model_cards()
        self.assertEqual(primary.route, "standard_primary")
        self.assertEqual(fallback.route, "plan_fallback")
        self.assertEqual(primary.model, fallback.model)
        self.assertEqual(primary.model, api.SEEDREAM_5_PRO_MODEL)
        self.assertEqual(primary.base_url, api.PRIMARY_BASE_URL)
        self.assertEqual(fallback.base_url, api.PLAN_BASE_URL)

    def test_plan_key_reuses_primary_key_when_blank(self) -> None:
        value = config()
        value["fallback_model_card"]["api_key"] = ""
        primary, fallback = api.ArkImageClient(value).model_cards()
        self.assertEqual(fallback.api_key, primary.api_key)

    def test_auto_max_is_card_specific_and_within_intrinsic_cap(self) -> None:
        request = api.aspect_to_size("16:9")
        primary, _ = api.ArkImageClient(config(primary_max=3_000_000)).model_cards()
        size = api._effective_size(primary, request)
        width, height = (int(item) for item in size.split("x"))
        self.assertEqual(request.mode, "auto_max")
        self.assertGreaterEqual(width * height, api.SEEDREAM_5_PRO_MIN_PIXELS)
        self.assertLessEqual(width * height, 3_000_000)

    def test_user_fixed_size_is_never_silently_resized(self) -> None:
        request = api.aspect_to_size("2048x2048")
        primary, _ = api.ArkImageClient(config()).model_cards()
        self.assertEqual(request.mode, "user_fixed")
        self.assertEqual(api._effective_size(primary, request), "2048x2048")

    def test_incompatible_primary_card_is_skipped_without_api_call(self) -> None:
        class FakeClient(api.ArkImageClient):
            def __init__(self, value: dict):
                super().__init__(value)
                self.routes: list[str] = []

            async def _post(self, card, body, timeout):
                self.routes.append(card.route)
                return {"data": [{"url": "https://example.invalid/output.png"}]}

        client = FakeClient(config(primary_max=3_000_000))
        result = asyncio.run(
            client.generate("test", api.aspect_to_size("2048x2048"), [])
        )
        self.assertEqual(result[4], "plan_fallback")
        self.assertEqual(result[5], "2048x2048")
        self.assertEqual(client.routes, ["plan_fallback"])

    def test_quota_error_rotates_from_standard_to_plan(self) -> None:
        class FakeClient(api.ArkImageClient):
            def __init__(self, value: dict):
                super().__init__(value)
                self.routes: list[str] = []

            async def _post(self, card, body, timeout):
                self.routes.append(card.route)
                if card.route == "standard_primary":
                    raise api.ImageApiError("HTTP 429: SetLimitExceeded")
                return {"data": [{"url": "https://example.invalid/output.png"}]}

        client = FakeClient(config())
        result = asyncio.run(
            client.generate("test", api.aspect_to_size("landscape"), [])
        )
        self.assertTrue(result[3])
        self.assertEqual(result[4], "plan_fallback")
        self.assertEqual(result[1], 2)
        self.assertEqual(client.routes, ["standard_primary", "plan_fallback"])

    def test_unknown_or_timeout_error_does_not_duplicate_request(self) -> None:
        class FakeClient(api.ArkImageClient):
            def __init__(self, value: dict):
                super().__init__(value)
                self.routes: list[str] = []

            async def _post(self, card, body, timeout):
                self.routes.append(card.route)
                raise api.ImageApiError("图片 API 请求超时")

        client = FakeClient(config())
        with self.assertRaisesRegex(api.ImageApiError, "超时"):
            asyncio.run(client.generate("test", api.aspect_to_size("landscape"), []))
        self.assertEqual(client.routes, ["standard_primary"])

    def test_seedream_5_pro_reference_limit_is_ten(self) -> None:
        client = api.ArkImageClient(config())
        with self.assertRaisesRegex(api.ImageConfigError, "最多接受 10 张"):
            client.preflight(
                "test",
                api.aspect_to_size("square"),
                ["data:image/png;base64,x"] * 11,
            )

    def test_request_body_is_single_image_seedream_pro(self) -> None:
        client = api.ArkImageClient(config())
        primary, _ = client.model_cards()
        body = client._body(primary, "test", "2048x2048", [])
        self.assertEqual(body["model"], api.SEEDREAM_5_PRO_MODEL)
        self.assertEqual(body["size"], "2048x2048")
        self.assertNotIn("sequential_image_generation", body)
        self.assertNotIn("n", body)


if __name__ == "__main__":
    unittest.main()
