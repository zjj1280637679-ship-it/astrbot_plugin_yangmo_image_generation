# AI 图片与视频生成及原图交付

给 AstrBot 主 Agent 增加一层轻量生成 harness：图片使用 Seedream，视频使用 `doubao-seedance-1-0-pro-250528`。两个能力都遵循 AstrBot 原生 Skill 渐进披露，Tool 可随时直接调用；默认先立即短通知，再执行真实生成并自动交付。Agent 仍可按上下文关闭预告、延迟交付、做候选比较、继续编辑或穿插其他工具。

交流与反馈：**QQ 群 916646029**

## 核心设计

```text
AstrBot Agent
  ├─ 决定是否检索 / 分析 / 说话 / 调其他工具
  ├─ 决定何时生成图片或视频
  ├─ 决定快速直出还是内部比较
  └─ 决定是否关闭默认预告或延迟交付

Native Skills
  ├─ image-generation
  └─ video-generation

Generation Harness
  ├─ generate_image / send_generated_images
  ├─ generate_video / send_generated_videos
  ├─ 默认即时短通知
  ├─ 默认自动交付
  └─ 结果重新回到同一个 Agent loop
```

**Agent 决定行为，Skill 提供经验，Tool 提供能力，Harness 承担等待期反馈、API 调用、持久化和机械交付。**

插件没有 `prepare`、一次性句柄或固定工具顺序。

## 图片能力

`generate_image(prompt, refs, count, aspect, auto_send=true, announce=true)`：

- 文生图、编辑、合成；
- `current` 或本插件 `genimg:` 作为参考图；
- 默认即时预告并自动发送原文件；
- `auto_send=false` 可用于候选比较；
- 返回内部预览和稳定 `genimg:` 引用。

`send_generated_images(refs)` 用于补发、重发或延迟交付。

## Seedance 视频能力

默认模型：

```text
doubao-seedance-1-0-pro-250528
```

`generate_video(prompt, first_frame, duration, ratio, resolution, return_last_frame, auto_send, announce)` 当前稳定暴露：

- 文生视频；
- 单首帧图生视频；
- 5 秒 / 10 秒；
- `adaptive / 1:1 / 16:9 / 4:3 / 21:9 / 9:16 / 3:4`；
- `480p / 720p / 1080p`；
- 方舟异步任务创建 + 轮询；
- 成功后下载 MP4 到插件私有存储；
- 默认自动作为 AstrBot `Video` 消息发送；
- 返回 `genvideo:`；
- 请求 `return_last_frame=true` 时，若服务端返回尾帧 URL，会保存为 `genframe:`，可直接作为下一段视频首帧。

首尾帧同时约束在资料中存在口径差异，因此 v0.3.0 **不把它冒充为稳定能力**。

## 视频首帧来源与插件互操作

`first_frame` 支持：

```text
""                  文生视频
current             当前用户消息第一张图
genimg:...          本插件历史生成图片
genframe:...        本插件历史视频尾帧
resolved            本轮最近一个工具返回的 ImageContent
```

`resolved` 是和其他插件协作的关键，但不会产生硬依赖。

例如你的群聊图片定位插件已经提供：

```text
ctximg:...
→ resolve_context_images(...)
→ 原图作为 ImageContent 回到当前 AstrBot Agent
→ generate_video(first_frame="resolved")
```

本插件只观察当前工具结果里的公开图片内容；**不会 import 其他插件，不会读取其他插件数据库，也不会自行解释 `ctximg:` 私有提取码。** 因此：

- 单独安装：当前消息图片 + 文生视频照常工作；
- 与图片定位/搜索插件一起装：可先解析历史图片，再作为首帧；
- 与其他插件一起装：各自状态和存储仍独立，不抢工具名、不共享数据库。

原则是：**跨插件传递公开内容，不跨插件偷读内部状态。**

## 视频提示词

Seedance 提示词重点描述“变化”，推荐组织：

```text
主体状态
→ 动作变化
→ 环境响应
→ 镜头运动
→ 时间顺序
→ 光影/风格
→ 连续性约束
```

首帧图生视频时，少重复静态画面，多描述接下来怎么动。例如：

> 保持首帧人物身份、服装、翅膀结构和背景连续。人物先缓慢抬头，随后展开双翼；羽毛受气流轻微抖动，衣摆自然摆动。镜头由中景缓慢后移，最后人物离地上升。不要新增人物，不要突然换景，不改变面部与服装设计。

## 上下文自适应

快速任务：

```text
用户：立刻做个视频，越快越好。
Agent → generate_video(announce=true, auto_send=true)
Harness → 立即短通知 → 创建任务 → 轮询 → 下载 → 自动发视频
```

候选任务：

```text
用户：先做三版，自己比较，只发最好的一版。
Agent 第一次可预告
→ generate_video(announce=false, auto_send=false)
→ 看预览
→ 最多继续本次任务要求的次数
→ send_generated_videos(best)
```

“最多 N 次”属于当前 Agent 任务预算，不是插件全局重试规则。

## 工具

- `generate_image`
- `send_generated_images`
- `generate_video`
- `send_generated_videos`
- `list_generation_capabilities`
- `list_image_capabilities`（保留兼容旧调用）

## 配置

图片与视频默认共用 `ark_api_key`。如果视频单独使用另一把 Key，可填写 `video_api_key`。

视频相关配置：

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `video_model` | `doubao-seedance-1-0-pro-250528` | Seedance 视频模型 |
| `video_base_url` | 方舟 `/api/v3` | 视频任务 API 基础地址 |
| `video_api_key` | 空 | 留空复用 `ark_api_key` |
| `video_poll_interval_seconds` | `4` | 任务状态轮询间隔 |
| `video_timeout_seconds` | `600` | 单次 Tool 等待上限 |
| `max_video_download_mb` | `256` | 单视频下载上限 |
| `max_video_store_bytes` | `8589934592` | 视频/尾帧私有存储上限 |

视频生成使用方舟异步接口；Tool 等待超时不等于服务端任务已经取消，错误回执会保留 `task_id`（如果已经成功创建）。

## 独立性与隐私

- 图片存储、视频存储、尾帧存储都位于本插件数据目录；
- `genimg:`、`genvideo:`、`genframe:` 按当前会话 scope 隔离；
- API Key 不写入日志、回执或仓库；
- 其他插件返回的图片只在当前事件中作为临时互操作内容使用，不持久化对方提取码协议；
- 本插件不修改 AstrBot 人格、主历史或其他插件配置；
- 视频/图片预览会作为 Tool Result 交给当前主模型，如果主模型是云服务，相应预览可能发送给该模型提供商。

## 安装

AstrBot `>=4.26.1`。安装仓库后填写方舟 Key 并重载插件即可。

仓库：

```text
https://github.com/zjj1280637679-ship-it/astrbot_plugin_yangmo_image_generation
```

## 许可

GNU Affero General Public License v3.0 或更高版本，见 `LICENSE`。
