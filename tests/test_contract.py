from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_schema_contains_only_two_secret_model_cards(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("primary_model_card", schema)
        self.assertIn("fallback_model_card", schema)
        self.assertTrue(schema["primary_model_card"]["items"]["api_key"]["secret"])
        self.assertTrue(schema["fallback_model_card"]["items"]["api_key"]["secret"])
        self.assertFalse(any("video" in key for key in schema))

    def test_live_plugin_surface_is_image_only(self) -> None:
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        init = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("generate_video", main)
        self.assertNotIn("send_generated_videos", main)
        self.assertNotIn("video", init.lower())
        self.assertFalse((ROOT / "video.py").exists())
        self.assertFalse((ROOT / "video_store.py").exists())

    def test_tool_descriptions_are_discriminative_for_stage_one(self) -> None:
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("不要用于搜索已有图片", main)
        self.assertIn("只发送已存在的 genimg 图片，不生成新图", main)
        self.assertIn("生成请求不要先调用", main)

    def test_two_stage_tool_surface_has_three_unambiguous_schemas(self) -> None:
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        tools: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                if not isinstance(decorator.func, ast.Attribute):
                    continue
                if decorator.func.attr != "llm_tool":
                    continue
                name = next(
                    (
                        keyword.value.value
                        for keyword in decorator.keywords
                        if keyword.arg == "name"
                        and isinstance(keyword.value, ast.Constant)
                    ),
                    node.name,
                )
                tools[str(name)] = [arg.arg for arg in node.args.args[2:]]

        self.assertEqual(
            set(tools),
            {"generate_image", "send_generated_images", "list_image_capabilities"},
        )
        self.assertEqual(
            tools["generate_image"],
            ["prompt", "refs", "aspect", "auto_send", "announce"],
        )
        self.assertEqual(tools["send_generated_images"], ["refs"])
        self.assertEqual(tools["list_image_capabilities"], [])

    def test_skill_declares_two_stage_contract_and_links_resources(self) -> None:
        skill = (ROOT / "skills/image-generation/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertTrue(skill.startswith("---\nname: image-generation\n"))
        self.assertIn("AstrBot 的 `skills_like` 工具模式本身分两阶段", skill)
        self.assertIn("不要添加固定的 prepare 步骤", skill)
        self.assertIn("status=generated_but_delivery_failed", skill)
        for resource in (ROOT / "skills/image-generation/references").glob("*.md"):
            self.assertIn(f"references/{resource.name}", skill)


if __name__ == "__main__":
    unittest.main()
