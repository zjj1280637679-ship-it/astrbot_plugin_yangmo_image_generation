---
name: image-generation
description: 当用户需要生成、编辑、重绘、合成、修复、扩图、设计海报/角色/信息图/产品图或以参考图继续创作时使用。工具本身始终可直接调用；本 Skill 只提供按需工作方法，不构成调用前置条件。
---

# 图片生成与编辑

这是 AstrBot 原生 Skill。遵循 AstrBot 的渐进披露：初始只暴露本 Skill 的名称和描述；命中后读取本文件；只有任务确实需要时，再读取下面直接引用的专项资料。不要一次加载全部资料。

## 核心原则

- 图片工具没有 `prepare`、句柄或强制状态门。AI 可在任何合适的推理步骤直接调用 `generate_image`、`send_generated_images` 或 `list_image_capabilities`。
- Skill 是工作方法，不是权限门；Tool 是执行接口；生成与发送是彼此独立的动作。
- `generate_image` 返回 `genimg:` 引用和内部预览，但不会自动把图片发进聊天。需要交付原文件时再调用 `send_generated_images`。
- 编辑当前消息图片时使用 `refs=["current"]`；继续编辑本插件历史生成物时使用相应 `genimg:` 引用。
- 不要为了遵守流程而多调用工具。任务足够明确时可以直接生成；仅在能力、模型、画幅或引用规则不确定时查询 `list_image_capabilities`。
- 生成失败后可根据错误直接修正并再次调用，不需要重新准备。

## 按需读取

只读取与当前任务直接相关的最少资料：

- 通用构图与场景：[`references/scene_composition.md`](references/scene_composition.md)
- 图片编辑、修复、合成：[`references/editing_compositing.md`](references/editing_compositing.md)
- 插画与角色：[`references/illustration_character.md`](references/illustration_character.md)
- 人像摄影：[`references/portrait_photography.md`](references/portrait_photography.md)
- 海报与文字排版：[`references/poster_typography.md`](references/poster_typography.md)
- 产品与广告：[`references/product_advertising.md`](references/product_advertising.md)
- 信息图与图解：[`references/infographic_diagram.md`](references/infographic_diagram.md)
- 游戏 UI / 资产：[`references/game_ui_assets.md`](references/game_ui_assets.md)
- 依赖实时知识的视觉任务：[`references/realtime_knowledge_visual.md`](references/realtime_knowledge_visual.md)

## 工具使用

### `generate_image`

直接生成、编辑或合成图片。把用户当前完整意图收敛成 `prompt`，按需要传入 `refs`、`count` 和 `aspect`。返回的预览用于当前 AI 判断结果质量；返回的 `genimg:` 是后续继续编辑或发送原图的稳定引用。

### `send_generated_images`

把一个或多个 `genimg:` 对应的原文件发送到当前聊天。只在确实需要交付时调用；发送失败时可以重试同一引用，不需要重新生成。

### `list_image_capabilities`

只读能力查询。用于不确定模型、画幅、参考图边界或输出数量时，不是生成前置步骤。

## 质量判断

生成后观察内部预览。如果已经满足用户目标，可以发送；如果存在明确可修正的问题，可以基于同一上下文再次生成/编辑。不要因为存在“流程”而无意义重画，也不要把内部预览误认为已经交付给用户。
