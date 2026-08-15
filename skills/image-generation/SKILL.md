---
name: image-generation
description: 当用户需要生成、编辑、重绘、合成、修复、扩图、设计海报/角色/信息图/产品图或以参考图继续创作时使用。图片工具可随时直接调用；默认先立即发送简短开始通知，生成后自动发送原图。本 Skill 只提供按需工作方法，不构成调用前置条件。
---

# 图片生成与编辑

这是 AstrBot 原生 Skill。Skill 是工作方法，不是权限门；Tool 是执行接口；AstrBot 主 Agent 决定何时生成、是否说话、是否调用其他工具以及是否继续推理。

## 核心原则

- `generate_image` 没有 prepare、句柄或固定前后流程，可在任意合适步骤直接调用。
- 默认 `announce=true`：开始执行就发送很短的处理通知；若 Agent 已预告或用户要求纯图片可设 `false`。
- 默认 `auto_send=true`：成功后 harness 自动交付原文件，同时把 `genimg:` 与内部预览返回当前 Agent。
- 候选比较、继续编辑、内部检查或暂不发送时可显式 `auto_send=false`。
- 编辑当前上传图或明确回复/引用图时优先 `refs=["current"]`；引用快速路径失败后才调用外部图片定位/解析工具并使用 `resolved`。
- 继续编辑历史生成物使用对应 `genimg:`。

## 像素与画幅：用户授权边界

把“构图比例”和“像素尺寸”分开处理。

- 用户没有明确要求像素/分辨率时，Agent 只选择 `landscape`、`portrait`、`square`、`photo`、`wide` 或 `W:H` 这类构图画幅，不自行填写 `WIDTHxHEIGHT`。
- AUTO_MAX 是程序默认：每个候选图片模型按该模型自己的 `image_model_max_pixels` 最大像素预算，在当前画幅下计算最大合法尺寸。不同模型可以有不同最大像素，这是模型环境变量，不要求彼此统一。
- 不能因为“越快越好”“简单一点”“随便画”“节省时间”等抽象要求自行降低像素；这些要求只影响规划长度等策略，除非用户同时明确要求降低分辨率。
- 只有用户明确提出像素/分辨率要求，例如“1024×1024”“按 2048x2048 输出”“降低到某个明确尺寸”，Agent 才可把 `aspect` 写成 `WIDTHxHEIGHT`。这代表 USER_FIXED。
- USER_FIXED 必须原样满足：当前模型能力不足时程序零 API 调用跳过该模型并尝试后续兼容模型；不允许静默缩小用户指定尺寸。
- 设置页中的 `image_model_max_pixels` 可以按模型独立修改 AUTO_MAX 上限。它改变客观能力环境，不改变上述用户授权规则。

## 引用 / 回复图片的最短路径

QQ 回复旧图片时，不默认先搜索历史。`current` 顺序为：当前直接图片 → `Reply.chain` 已附带原图 → AstrBot quoted-message parser 的短超时解析 → 失败后才走 Agent 外部 resolver / `resolved`。

如果用户同时直接上传新图并回复旧图，直接上传图优先，尤其视频 `first_frame=current` 不会被旧引用图覆盖。

## 默认响应节奏

普通生图通常是：Agent 决定生成 → `generate_image(announce=true, auto_send=true)` → harness 立即短通知 → 图片 API → 自动发送原文件 → preview + genimg 回到同一个 Agent loop。没有要求 Agent 必须先说话或生成后必须总结。

## 根据上下文自适应

- “立刻、马上、越快越好”：减少无价值规划并直接生成，但不要因此降低像素。
- “只发图、不要文字”：`announce=false`。
- “先构思、超精细、多方案、先比较、选最好”：可增加有效规划；候选可 `announce=false, auto_send=false`。
- “先别发、只给最好的一张”：候选阶段 `auto_send=false`，最后发送选中的 `genimg:`。
- 用户给“最多 N 次”等预算时由 Agent 在当前任务遵守，不固化成全局重试规则。

## Prompt 策略

优先追求语义密度和约束完整性，不机械追求长度。明确主体、动作、环境、构图、镜头、光线、材质、色彩、文字和必须避免的错误。用户要求“尽量详细”时扩展有意义约束，不用同义反复填充。

## 工具语义

`generate_image` 参数包括 `prompt`、`refs`、`count`、`aspect`、`auto_send`、`announce`。`aspect` 通常承载构图画幅；只有用户明确指定像素时才使用 `WIDTHxHEIGHT`。

`send_generated_images` 用于 `auto_send=false` 后择优交付、补发、重发或稍后交付，不是固定下一步。

`list_image_capabilities` 是只读能力查询，不是生成前置步骤。

## 质量判断

生成后内部预览会回到当前 Agent。默认自动发送的图片已经交付，但 Agent 仍可据此继续评价、解释、编辑、调用其他工具或结束。若任务明确要求先检查再交付，应在生成前使用 `auto_send=false`。
