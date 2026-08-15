# 更新日志

本项目采用语义化版本。

## [0.3.3] - 2026-08-15

### Added

- 新增视频默认参数设置：`video_default_duration`、`video_default_ratio`、`video_default_resolution`、`video_default_return_last_frame`、`video_default_auto_send`、`video_default_announce`。
- 仅当 Agent 没有显式传对应参数时，harness 才把插件设置中的默认值注入 `generate_video`；单次任务明确指定值始终优先。

### Changed

- 视频出厂默认时长从 5 秒调整为 10 秒。
- `first_frame` 继续保持任务驱动，不提供全局默认，避免上下文里碰巧存在图片时把普通文生视频静默改成图生视频。
- CogVideoX-Flash 的 `quality` / `fps` 继续沿用原有独立设置。

## [0.3.2] - 2026-08-15

### Added

- `refs=["current"]` 与 `generate_video(first_frame="current")` 新增 QQ 引用/回复图片快速路径。
- 第一层直接读取 AstrBot `Reply.chain` 已附带的图片，不产生额外 OneBot 查询。
- 若 `Reply.chain` 没有原图，则复用 AstrBot 4.27.3 自带 `extract_quoted_message_images`，通过官方 quoted-message parser / OneBot resolver 取回被引用图片。
- 新增 `quoted_image_fastpath_timeout_seconds`，默认 3 秒；远程引用解析超时后立即降级，不阻断原有 Agent `ctximg → resolver → resolved` 兜底。

### Changed

- `current` 现在语义为“当前消息直接图片优先 + 明确引用图片作为低优先级补充”。直接上传图片仍保持第一顺位，避免引用图悄悄覆盖视频首帧。
- 引用图片快速路径严格限制在剩余参考图预算内，不会因为消息额外带 Reply 而把原本合法的 14 张直接参考图请求推到超限失败。
- 引用解析中的格式错误、OneBot 失败或超时均按 fail-soft 处理；不会调用图片理解模型，也不会让群聊图片描述模型的 400/contentFilter 阻断生图/视频主链。

## [0.3.1] - 2026-08-15

### Changed

- 视频默认路线改为 `cogvideox-flash`（配置智谱 Key 时），Seedance `doubao-seedance-1-0-pro-250528` 作为备用/独立路线。
- 两条视频路线均保持无声；不再引入 Seedance 1.5 Pro，也不自动追加声音提示词。
- 轮换只发生在创建任务被明确拒绝且可安全重试时；网络超时等提交状态未知场景不会重复提交视频任务。

## [0.3.0] - 2026-08-15

### Added

- 新增 `generate_video`：使用 `doubao-seedance-1-0-pro-250528`，支持文生视频与单首帧图生视频。
- 新增 `send_generated_videos`，用于补发、重发或延迟交付 `genvideo:` 视频。
- 新增 `video-generation` 原生 Skill，按 Seedance 的动作、时间顺序、运镜和连续性方式组织提示词。
- 新增 `genvideo:` 私有视频存储与 `genframe:` 尾帧存储；尾帧可直接作为下一段视频首帧。
- 新增方舟视频异步任务客户端：创建任务、轮询 queued/running、读取成功/失败状态、下载 MP4 与可选尾帧。
- 新增 `list_generation_capabilities`，统一暴露图片/视频能力和互操作语义。

### Interop

- 新增松耦合 Tool 图片桥：观察当前事件中其他工具返回的公开 `ImageContent`，保存为仅当前事件有效的临时图片；`generate_video(first_frame="resolved")` 可把最近解析出的图片作为首帧。
- 不 import 图片定位/搜索插件，不读取其他插件数据库，也不解析其私有提取码；与 `ctximg:` 等能力协作时，由 AstrBot Agent 先调用对应公开解析工具，再使用 `resolved`。
- 单独安装时仍完整支持文生视频、当前消息首帧、`genimg:` 和 `genframe:`；联合安装不会共享持久状态或抢占其他插件工具名。

### Changed

- 插件展示名升级为“AI 图片与视频生成及原图交付”。
- 图片与视频均保持 harness 语义：默认即时短通知、默认自动交付，但 Agent 可通过 `announce=false` / `auto_send=false` 关闭机械默认。
- API Key 默认复用 `ark_api_key`；可选单独配置 `video_api_key`。
- v0.3.0 暂不暴露 Seedance 1.0 Pro 的双首尾帧约束，避免在官方资料口径不一致时伪造稳定能力。

## [0.2.3] - 2026-08-15

### Changed

- `generate_image` 新增 `announce` 参数，默认 `true`：完成最基本的参数检查后立即发送简短开始通知，再进入参考图解析和图片 API 请求，减少长耗时生图期间的冷等待。
- 默认预告由 harness 执行，不要求 AstrBot Agent 先生成固定话术；如果 Agent 已经自行预告，或用户明确要求只发图片/不要文字，可设 `announce=false`。
- 预告发送失败不会阻断图片生成，仍继续执行后续 API、存储和交付逻辑。

## [0.2.2] - 2026-08-15

### Changed

- 明确插件定位为 AstrBot 主 Agent 的轻量图片 harness。
- `generate_image` 保持默认 `auto_send=true`，`auto_send=false` 用于候选比较、内部检查、继续编辑和择优交付。
- 精简工具回执，只保留事实状态，减少上下文噪声。
- 原生 Skill 增加上下文自适应策略。

## [0.2.1] - 2026-08-15

### Changed

- `generate_image` 新增 `auto_send`，默认 `true`；成功生成后自动把原文件发送到当前聊天。
- 保留 `auto_send=false`，允许主 Agent 先观察、比较、继续编辑或稍后择优交付。
- `send_generated_images` 降级为补发、重发和延迟交付基础设施。

## [0.2.0] - 2026-08-15

### Changed

- 改为完全使用 AstrBot 原生 Skill 渐进披露。
- `generate_image` 改为可直接调用，不再要求 `prepare_image_generation` 或 `image_task_handle`。
- 删除“先准备、再消费一次性句柄”的硬性状态机。

### Removed

- `prepare_image_generation` LLM Tool。
- 自定义 `lifecycle.py` prepare/consume 句柄协议。
- 自定义 `skills.py + knowledge/manifest.json` 动态知识注入层。
- 旧 `image-intent-router` Skill。

## [0.1.0] - 2026-08-07

### Added

- 加入商场图标与 QQ 交流群 916646029。
- 提供图片生成、预览和原图交付能力。
- 支持当前消息图片与本插件 `genimg:` 生成物作为参考图。

### Security

- API Key 默认值为空，工具回执和日志不输出密钥。
- 生成物按会话作用域隔离，并受保留时间和空间上限约束。
