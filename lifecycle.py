from __future__ import annotations

import secrets

from . import skills

EVENT_EXTRA_KEY = "_yangmo_independent_image_task"


def prepare(event, announcement: str, related_skill_ids=None) -> dict:
    text = " ".join(str(announcement or "").split())
    if not text:
        raise ValueError("announcement 不能为空")
    if len(text) > 180:
        raise ValueError("announcement 最多 180 个字符")
    activation = skills.activate_best_effort(related_skill_ids)
    handle = secrets.token_urlsafe(12)
    state = {"handle": handle, "announcement": text, "consumed": False}
    setter = getattr(event, "set_extra", None)
    if not callable(setter):
        raise RuntimeError("当前事件不支持图片准备状态")
    setter(EVENT_EXTRA_KEY, state)
    return {
        "handle": handle,
        "announcement": text,
        "knowledge_version": activation["knowledge_version"],
        "knowledge_status": activation["knowledge_status"],
        "selection_status": activation["selection_status"],
        "selection_warnings": activation["selection_warnings"],
        "activated_skill_ids": activation["activated_skill_ids"],
        "activation_markdown": skills.render_activation(activation),
    }


def consume(event, handle: str) -> tuple[dict | None, str | None]:
    getter = getattr(event, "get_extra", None)
    state = getter(EVENT_EXTRA_KEY, None) if callable(getter) else None
    if not isinstance(state, dict):
        return None, "请先调用 prepare_image_generation，再在下一模型步骤生成。"
    if state.get("consumed"):
        return None, "当前图片准备句柄已经消费；不要重复生成。"
    if not handle or not secrets.compare_digest(str(handle), str(state.get("handle") or "")):
        return None, "image_task_handle 与当前事件的准备回执不匹配。"
    state["consumed"] = True
    return state, None

