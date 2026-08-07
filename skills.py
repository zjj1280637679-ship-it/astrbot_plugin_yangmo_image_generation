from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "knowledge"
MANIFEST = ROOT / "manifest.json"


def _load_manifest() -> dict:
    value = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
    if value.get("schema_version") != "image-skill-kb-v3":
        raise ValueError("图片技能清单版本不受支持")
    return value


def catalog_ids() -> list[str]:
    try:
        return [str(row["id"]) for row in _load_manifest()["types"]]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def _document(row: dict, kind: str) -> dict:
    path = ROOT / str(row["path"])
    return {
        "id": str(row["id"]),
        "kind": kind,
        "path": str(path),
        "markdown": path.read_text(encoding="utf-8-sig"),
    }


def activate(related_skill_ids=None) -> dict:
    manifest = _load_manifest()
    rows = {str(row["id"]): row for row in manifest["types"]}
    requested = list(dict.fromkeys(str(x) for x in (related_skill_ids or []) if str(x)))
    selected = [value for value in requested if value in rows][:3]
    warnings = []
    if any(value not in rows for value in requested):
        warnings.append("unregistered_selection_ignored")
    if len(requested) > 3:
        warnings.append("selection_above_maximum_truncated")
    fixed = [str(value) for value in manifest.get("fixed_related_type_ids") or []]
    ids = list(dict.fromkeys(fixed + selected))
    master = _document(manifest["master"], "master")
    related = [_document(rows[value], "work_type") for value in ids]
    return {
        "knowledge_version": str(manifest["knowledge_version"]),
        "knowledge_status": "ready",
        "selection_status": "degraded" if warnings else "complete",
        "selection_warnings": warnings,
        "activated_skill_ids": [master["id"]] + [row["id"] for row in related],
        "master": master,
        "related": related,
    }


def activate_best_effort(related_skill_ids=None) -> dict:
    try:
        return activate(related_skill_ids)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {
            "knowledge_version": "unavailable",
            "knowledge_status": "unavailable",
            "selection_status": "degraded",
            "selection_warnings": ["knowledge_bundle_unavailable"],
            "activated_skill_ids": [],
            "master": None,
            "related": [],
        }


def render_activation(activation: dict) -> str:
    docs = []
    if isinstance(activation.get("master"), dict):
        docs.append(activation["master"])
    docs.extend(activation.get("related") or [])
    warnings = ",".join(activation["selection_warnings"]) or "none"
    header = (
        f"IMAGE_SKILL_KB_ACTIVATED version={activation['knowledge_version']} "
        f"status={activation['knowledge_status']} "
        f"selection={activation['selection_status']} warnings={warnings}"
    )
    if not docs:
        return header + "\n技能资料暂不可用；这不构成拒绝生成的理由。"
    return "\n\n".join(
        [header]
        + [
            f'<image_skill id="{row["id"]}" kind="{row["kind"]}">\n'
            f'{row["markdown"]}\n</image_skill>'
            for row in docs
        ]
    )
