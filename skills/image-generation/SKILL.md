---
name: image-generation
description: 当用户需要生成、编辑、重绘、合成、修复、扩图、设计海报/角色/信息图/产品图或以参考图继续创作时使用。工具始终可直接调用，默认生成后自动发送原图；本 Skill 只提供按需工作方法，不构成调用前置条件。
---

# 图片生成与编辑

这是 AstrBot 原生 Skill。遵循 AstrBot 的渐进披露：初始只暴露本 Skill 的名称和描述；命中后读取本文件；只有任务确实需要时，再读取下面直接引用的专项资料。不要一次加载全部资料。

## 核心原则

- 图片工具没有 `prepare`、句柄或强制状态门。AI 可在任何合适的推理步骤直接调用 `generate_image`、`send_generated_images` 或 `list_image_capabilities`。
- Skill 是工作方法，不是权限门；Tool 是执行接口；插件提供的是 harness 基础设施，不规定 Agent 必须怎样组织一次回复。
- `generate_image` 默认 `auto_send=true`：生成成功后插件立即把原文件发到当前聊天，同时把 `genimg:` 引用与内部预览返回给当前 AI。
- 如果当前 Agent 明确需要先观察、比较候选、继续编辑或暂不交付，可调用 `generate_image(..., auto_send=false)`；之后可继续生成、调用其他工具、说话，或需要时再用 `send_generated_images` 补发。
- 模型不需要在发图前说预告，也不需要在发图后追加说明。可以只发图，也可以在工具调用前后自由穿插自然语言、检索、分析或其他 Agent 工具。
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

直接生成、编辑或合成图片。把用户当前完整意图收敛成 `prompt`，按需要传入 `refs`、`count` 和 `aspect`。

默认不需要模型负责交付：`auto_send=true` 时生成成功即发送原文件。返回的内部预览用于当前 AI 判断结果质量；返回的 `genimg:` 是后续继续编辑或重发原图的稳定引用。

当任务本身需要“先看再决定”“先生成候选但不要发”“继续编辑后再交付”时，模型可以显式传 `auto_send=false`。这只是一个可选控制，不是新的生命周期。

### `send_generated_images`

重发或补发一个或多个 `genimg:` 对应的原文件。因为 `generate_image` 默认自动发送，本工具主要用于 `auto_send=false`、自动发送部分失败、用户要求重发、或稍后交付历史生成物；它不是固定下一步。

### `list_image_capabilities`

只读能力查询。用于不确定模型、画幅、参考图边界、输出数量或交付行为时，不是生成前置步骤。

## Agent 自由

插件提供两种自由：

- **积极自由**：Agent 可以主动决定何时生成、是否先检索/分析、是否继续编辑、是否调用其他工具，以及是否在工具调用前后说话。
- **消极自由**：插件不要求预告、不要求总结、不要求固定工具顺序、不要求最终文本，也不要求 Skill 激活后才能使用图片工具。

默认自动发图只承担一个 harness 责任：当 Agent 已经决定调用 `generate_image` 且生成成功时，插件负责把产物交付出去，避免再要求模型完成一次机械的发送动作。

## 质量判断

生成后仍会把内部预览返回给当前 AI。AI 可以继续观察并决定是否编辑、解释、调用其他工具或直接结束；已经自动发送的图片不妨碍后续 Agent 行为。不要因为存在“流程”而无意义重画。
