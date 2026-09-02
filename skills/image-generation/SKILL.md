---
name: image-generation
description: 使用 Seedream 5 Pro 生成或编辑图片。用户要求画图、改图、重绘、合成、扩图、修复图片，或制作角色主体图、海报、信息图、产品图时使用；不用于搜索网上已有图片。支持当前/引用图片、外部工具解析图片和 genimg 历史生成物。
---

# 使用 Seedream 5 Pro 生成与编辑图片

## 执行主流程

1. 判断用户要“生成/编辑新图片”还是“查找已有图片”。只有前者使用 `generate_image`。
2. 确定参考图来源和顺序。没有参考图就是文生图；有参考图就是编辑、合成或重绘。
3. 把用户目标整理成完整提示词，保留身份、拓扑、位置、数量、文字等硬约束。
4. 调用 `generate_image`。默认让插件发送短通知、生成、保存并自动交付原图。
5. 读取工具结果中的 `status`、`delivery`、`route` 和 `genimg:`。只有 `status=ok` 且自动投递成功，才算交付闭环完成。
6. 若用户要求比较候选，分别生成时设置 `auto_send=false`，检查预览后用 `send_generated_images` 发送选中的 `genimg:`。

不要添加固定的 prepare 步骤。AstrBot 的 `skills_like` 工具模式本身分两阶段：第一阶段按工具名称和描述选择工具，第二阶段才提供参数 Schema。`generate_image` 的参数在第二阶段一次填完整即可。

## 选择参考图来源

- 当前消息直接上传图片，或用户明确回复/引用一张旧图：使用 `refs=["current"]`。
- 外部搜索、群聊图片定位或解析工具刚返回了公开 `ImageContent`：使用 `refs=["resolved"]`。
- 继续编辑本插件之前生成的图片：使用对应 `refs=["genimg:..."]`。
- 多张参考图可以按语义顺序组合，例如 `refs=["current", "genimg:..."]`。提示词中用“图1、图2”明确每张图的角色。

引用是严格约束：只要显式列出的任一来源不可用，工具会在调用付费 API 前失败。不要在参考图缺失时擅自用剩余图片继续生成。

`current` 的解析顺序是：当前直接图片 → `Reply.chain` 已附带图片 → AstrBot 原生 quoted-message 解析。快速路径失败时，再调用合适的外部图片定位/解析工具，然后传 `resolved`。

## 编写提示词

按任务需要明确以下内容，不用机械堆词：

- 主体：身份、数量、外观、服饰、材质。
- 关系：谁在前后左右、谁属于哪张参考图、身体部件连接关系。
- 状态与动作：姿态、视线、接触、受力和运动方向。
- 环境与构图：场景、景别、机位、画幅、留白。
- 光影与风格：光源、色彩、质感、媒介或摄影语言。
- 必须保持：人物身份、脸部、服装、背景、文字或其他不可变元素。

编辑任务优先写“改什么、保持什么”。多图合成必须说明每张图的职责，避免仅写“参考这些图”。

按类型需要时读取对应参考资料：

- 编辑、换装、合成、扩图：[editing_compositing.md](references/editing_compositing.md)
- 角色与插画：[illustration_character.md](references/illustration_character.md)
- 人像摄影：[portrait_photography.md](references/portrait_photography.md)
- 场景与镜头构图：[scene_composition.md](references/scene_composition.md)
- 海报与文字排版：[poster_typography.md](references/poster_typography.md)
- 产品广告：[product_advertising.md](references/product_advertising.md)
- 信息图与精确图示：[infographic_diagram.md](references/infographic_diagram.md)
- 游戏 UI 素材：[game_ui_assets.md](references/game_ui_assets.md)
- 需要实时知识支撑的视觉内容：[realtime_knowledge_visual.md](references/realtime_knowledge_visual.md)

## 画幅与像素

- 用户没有明确给出像素时，只传 `landscape`、`portrait`、`square`、`photo`、`wide` 或 `W:H`。插件按模型卡的像素预算计算最大合法尺寸，属于 AUTO_MAX。
- “快一点、简单一点、随便画”不等于用户授权降低分辨率。
- 只有用户明确提出 `1024x1024`、`2048×2048` 等尺寸时，才传 `WIDTHxHEIGHT`，属于 USER_FIXED。
- USER_FIXED 不会被静默缩小。模型卡无法满足时，工具在零 API 调用阶段报错或跳过该卡。

## 工具职责

- `generate_image`：生成或编辑一张新图片。普通 API 模型卡优先；只有明确额度、限流或过载错误才回退到 Plan API 模型卡。
- `send_generated_images`：发送已经存在的 `genimg:`，不生成新图。
- `list_image_capabilities`：只在用户询问模型卡、限制或可用能力时查询；不是生成前置步骤。

Seedream 5 Pro 每次工具调用只生成一张图。用户要多版时，按用户预算多次调用；若要求“只发最好的一张”，候选调用使用 `auto_send=false`。

## 处理结果

- `status=ok`：生成完成；再检查 `delivery.sent` 是否符合预期。
- `status=generated_but_delivery_failed`：图片已生成并有 `genimg:`，但用户没有收到。解释失败并可调用 `send_generated_images` 补发。
- `status=fail`：按 `error` 处理；不要声称图片已经生成或发送。
- `route=standard_primary`：普通 API 成功。
- `route=plan_fallback`：普通 API 明确受限后由 Plan API 成功接管。

若用户要求只发图片，设置 `announce=false`，成功自动交付后不要追加无关文字。
