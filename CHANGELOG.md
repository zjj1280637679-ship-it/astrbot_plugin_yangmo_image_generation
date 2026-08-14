# 更新日志

本项目采用语义化版本。

## [0.2.3] - 2026-08-15

### Changed

- `generate_image` 新增 `announce` 参数，默认 `true`：完成最基本的参数检查后立即发送简短开始通知，再进入参考图解析和图片 API 请求，减少长耗时生图期间的冷等待。
- 默认预告由 harness 执行，不要求 AstrBot Agent 先生成固定话术；如果 Agent 已经自行预告，或用户明确要求只发图片/不要文字，可设 `announce=false`。
- 预告发送失败不会阻断图片生成，仍继续执行后续 API、存储和交付逻辑。
- `list_image_capabilities` 增加默认交互反馈语义，明确短预告是可关闭的 UX 默认值而非强制工作流。
- 原生 Skill 和 README 增加普通快速路径、纯图片路径和多轮 Agentic 路径中的预告去重指导；插件仍不建立额外状态机。

## [0.2.2] - 2026-08-15

### Changed

- 明确插件定位为 AstrBot 主 Agent 的轻量图片 harness：Agent 决定行为，Skill 提供经验，Tool 提供能力，Harness 承担机械副作用。
- `generate_image` 保持默认 `auto_send=true`，但把 `auto_send=false` 明确定义为“把交付时机留给 Agent”，用于候选比较、内部检查、继续编辑和择优交付。
- 精简 `generate_image` 的工具回执，移除每次生成都重复返回的 `agent_freedom` 和 `available_actions` 说明，只保留生成、模型、调用、参考图和真实交付状态，减少上下文噪声和工具选择偏置。
- Tool 描述改为短而明确的动作语义，并明确每次生成会产生真实外部 API 请求和潜在费用。
- `list_image_capabilities` 增加 prompt 指导：当前没有可靠的模型级硬上限，不按假定上限百分比机械填充，优先高语义密度的自然语言约束。
- 原生 Skill 增加上下文自适应策略：速度优先时可直接生成；需要比较/择优时用 `auto_send=false`；尝试次数等预算由 AstrBot Agent 按当前任务自行遵守，不固化为插件模式。
- README 增加快速路径与 Agentic 路径示例，并同步默认自动交付语义。

## [0.2.1] - 2026-08-15

### Changed

- `generate_image` 新增 `auto_send`，默认 `true`；成功生成后自动把原文件发送到当前聊天。
- 保留 `auto_send=false`，允许主 Agent 先观察、比较、继续编辑或稍后择优交付。
- `send_generated_images` 从固定生命周期步骤降级为补发、重发和延迟交付基础设施。
- 生成结果仍返回 `genimg:` 与内部预览，自动发图不会终止 AstrBot 的后续 Agent loop。

## [0.2.0] - 2026-08-15

### Changed

- 改为完全使用 AstrBot 原生 Skill 渐进披露：初始只暴露 Skill 名称与描述，命中后读取 `SKILL.md`，再按需读取少量 `references/*.md`。
- `generate_image` 改为可直接调用，不再要求 `prepare_image_generation` 或 `image_task_handle`。
- `send_generated_images` 与 `list_image_capabilities` 同样不受固定生命周期约束，可由主 Agent 在任意合适步骤调用。
- 删除“先准备、再消费一次性句柄”的硬性状态机；Skill 只负责工作方法，不再充当权限门。
- 生成结果返回可选后续动作，不强迫 AI 必须发送、重画或继续某个固定步骤。
- 内部预览超时会显式终止 ffmpeg 子进程。

### Removed

- `prepare_image_generation` LLM Tool。
- 自定义 `lifecycle.py` prepare/consume 句柄协议。
- 自定义 `skills.py + knowledge/manifest.json` 动态知识注入层。
- 旧 `image-intent-router` Skill。

### Migration

- v0.1.x 调用方应移除 `prepare_image_generation` 与 `image_task_handle`，直接调用 `generate_image(prompt, refs, count, aspect)`。
- 原有 `genimg:` 存储、参考图解析、内部预览、原图发送和模型降级机制保持不变。

## [0.1.0] - 2026-08-07

### Added

- 加入 256×256 商场图标与 QQ 交流群 916646029。
- 提供准备、生成、预览和原图交付的完整图片任务生命周期。
- 支持当前消息图片与本插件 `genimg:` 生成物作为参考图。
- 内置总创作知识、按需作品技能、方舟普通接口与套餐接口兜底。

### Security

- API Key 默认值为空，工具回执和日志不输出密钥。
- 生成物按会话作用域隔离，并受保留时间和空间上限约束。

### Changed

- 完成商场元数据、用户说明、配置提示、计费边界与发布契约。
- 统一为唯一的准备、生成、预览、原图交付生命周期，移除直接生成并发送的命令旁路。
- 每个候选模型只请求一次；保留部分成功结果，不再循环补图或丢弃后整批重画。
- 增加官方尺寸、比例、参考图数量和单图大小边界，并让失败回执报告 `api_calls`。
