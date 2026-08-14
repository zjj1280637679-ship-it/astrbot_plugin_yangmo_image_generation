# 更新日志

本项目采用语义化版本。

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
