---
name: image-intent-router
description: 图片任务的轻量入口。只有用户确实要求生成、编辑或合成图片时才进入工具帧。
---

# 图片生成入口

图片任务使用同一主对话的完整上下文，不另开隔离对话。

状态机只有：S0 已接受 → S1 已准备 → S2 已生成 → S3 已发送 → END。

- S0→S1：调用 `prepare_image_generation`，`announcement` 写你准备先告诉用户的话，按作品类型选择最多 3 个 `related_skill_ids`。
- S1→S2：下一模型步骤调用 `generate_image`，原样传入真实 `image_task_handle`。不要复制 `$HANDLE` 占位符。
- S2→S3：看完工具返回的内部预览后，需要交付就调用 `send_generated_images`，填写真实 `genimg:` 提取码。
- 生成失败且没有 `genimg:`：句柄已消费；修正后重新准备。发送失败只重发同一 `genimg:`，不得再次生成。
- 用户没有图片任务时，不调用准备工具，也不加载完整图片知识。

