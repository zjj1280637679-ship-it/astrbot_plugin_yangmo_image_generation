---
name: image-generation
description: 当用户需要生成、编辑、重绘、合成、修复、扩图、设计海报/角色/信息图/产品图或以参考图继续创作时使用。图片工具可随时直接调用，默认生成后自动发送原图；本 Skill 只提供按需工作方法，不构成调用前置条件。
---

# 图片生成与编辑

这是 AstrBot 原生 Skill。遵循渐进披露：初始只暴露名称和描述；命中后读取本文件；只有任务确实需要时，再读取下面直接引用的最少专项资料。不要一次加载全部资料。

## 核心原则

- Skill 是工作方法，不是权限门；Tool 是执行接口；AstrBot 主 Agent 决定何时生成、是否说话、是否调用其他工具以及是否继续推理。
- `generate_image` 没有 `prepare`、句柄或固定前后流程，可在任意合适的 Agent 步骤直接调用。
- 默认 `auto_send=true`：生成成功后 harness 自动交付原文件，同时把 `genimg:` 引用与内部预览返回给当前 Agent。
- 当任务需要先比较候选、继续编辑、内部检查、择优交付或暂不发送时，显式使用 `auto_send=false`。之后可继续生成、调用其他工具、说话，或稍后用 `send_generated_images` 交付。
- 不要求发图前预告，不要求发图后总结，也不要求最终回复必须包含文字。用户只要图片时可以只发图片；复杂任务则可以自由穿插语言、检索、分析和其他 Agent 工具。
- 编辑当前消息图片时使用 `refs=["current"]`；继续编辑历史生成物时使用对应 `genimg:` 引用。
- 不要为了遵守流程而增加工具调用。任务足够明确时直接生成；只有能力、模型、画幅、参考图边界或交付语义不确定时才查询 `list_image_capabilities`。

## 根据上下文自适应

把用户的速度、质量、筛选和交付要求理解为 Agent 策略，而不是插件模式枚举。

- 用户强调“立刻、马上、越快越好、别解释”：减少无价值规划，直接调用 `generate_image`，通常保留 `auto_send=true`。
- 用户要求“先构思、超精细、多方案、先比较、选最好”：允许更长的推理和专项资料读取；候选生成通常使用 `auto_send=false`，看完内部预览后再决定继续编辑还是交付。
- 用户说“先别发、只给我最好的一张”：候选阶段使用 `auto_send=false`，最后只发送选中的 `genimg:`。
- 用户要求生成后点评、解释或继续操作：交付不会终止 Agent loop；看完返回的预览后继续完成自然语言或其他工具动作即可。
- 用户给出“最多 N 次”等预算时，由主 Agent 在当前任务中遵守，不把它固化成插件全局重试规则。

## Prompt 策略

优先追求**语义密度和约束完整性**，不要机械追求提示词长度。

- 用连贯自然语言明确主体、动作、环境、构图、镜头、光线、材质、色彩、文字和必须避免的错误。
- 用户要求“尽量详细”时，可以扩展有意义的视觉约束，但不要为了达到某个百分比填充同义反复或无效细节。
- 当前插件没有可靠的模型级硬提示词上限；如果用户要求“用到上限的 80%”之类定量目标，除非有可信的当前模型上限，否则把它理解为“高信息密度、充分展开但避免截断和信息稀释”。
- 如果能力查询以后提供了可靠的模型级硬上限，再以那个实际值作为预算；不要凭空假设。

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

## 工具语义

### `generate_image`

生成、编辑或合成图片。参数包括 `prompt`、`refs`、`count`、`aspect` 和 `auto_send`。

默认 `auto_send=true` 是 harness 的机械便利：Agent 一旦已经决定生成，插件负责把成功产物交付出去，避免额外再做一次没有认知价值的发送调用。

`auto_send=false` 不是“高级模式”，只是把交付时机留给 Agent。适合候选比较、继续编辑、内部检查和择优交付。

### `send_generated_images`

发送或重发已有 `genimg:` 对应的原文件。主要用于 `auto_send=false` 后的择优交付、自动发送失败后的补发、用户要求重发或稍后交付历史生成物。不是固定下一步。

### `list_image_capabilities`

只读能力查询。用于确认模型、画幅、参考图边界、输出数量、交付行为或当前 prompt 指导；不是生成前置步骤。

## 质量判断

生成后内部预览会回到当前 Agent。默认自动发送的图片已经交付，但 Agent 仍可据此继续评价、解释、编辑、调用其他工具或直接结束。

如果任务明确要求“先检查再交付”，应在生成前选择 `auto_send=false`；不要先自动发送再假装进行择优。
