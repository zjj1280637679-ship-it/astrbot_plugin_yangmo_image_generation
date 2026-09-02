# Seedream 5 Pro 图片生成与原图交付

为 AstrBot 主 Agent 提供纯图片生成、编辑、合成和原图交付能力。插件只保留两张能力相同的 Seedream 5 Pro 模型卡：普通 API 主卡与 Plan API 回退卡。

交流与反馈：**QQ 群 916646029**

## 路由

```text
图片请求
  → 普通 API · doubao-seedream-5-0-pro-260628
  → 仅在额度 / 限流 / 明确过载时回退
  → Plan API · doubao-seedream-5-0-pro-260628
  → 下载原图 → genimg 持久化 → 自动发送 → Tool Result 回到 Agent
```

网络超时或连接中断属于提交结果未知，不会为了回退而盲目重复一次可能计费的请求。

## AstrBot 两阶段工具模式

插件同时兼容 AstrBot 的 `full` 与 `skills_like` Tool Schema 模式。

- `full`：一次把工具描述和完整参数 Schema 交给模型。
- `skills_like`：第一阶段只提供工具名与描述，模型选中工具后，第二阶段再提供参数 Schema。

因此插件不再造 `prepare_image_generation` 一类前置工具。三个工具的第一阶段描述互斥，第二阶段参数说明自足：

- `generate_image`：生成或编辑一张新图片。
- `send_generated_images`：只发送已有 `genimg:`，不生成。
- `list_image_capabilities`：只读查询能力，不是生成前置步骤。

AstrBot v4.13.0 起提供 Skills-like 两阶段 Tool Schema；当前实现可见 [AstrBot ToolLoopAgentRunner](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/agent/runners/tool_loop_agent_runner.py) 和 [ToolSet](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/agent/tool.py)。

## 原生 Skill

插件内置 `skills/image-generation/SKILL.md`。AstrBot 初始只暴露 Skill 的名称与触发描述，命中后再读取完整操作手册。Skill 负责生成方法、参考图选择、提示词组织和结果闭环；Tool 负责真实 API、存储与发送。

参考：[AstrBot Skills 文档](https://docs.astrbot.app/use/skills.html)。

## 图片工具

### `generate_image`

```text
generate_image(prompt, refs, aspect, auto_send=true, announce=true)
```

- 文生图、图片编辑、多图合成；
- 每次调用生成一张 Seedream 5 Pro 图片；
- `refs` 支持 `current`、`resolved`、`genimg:...`；
- `current` 会优先读取当前上传图及 QQ Reply/引用图片；
- `resolved` 接收本轮最近一个外部工具返回的公开 `ImageContent`；
- 显式参考图采用严格语义，缺失任一来源都会在付费 API 前失败；
- 默认先短通知，成功后自动发送原图；
- `auto_send=false` 可用于候选比较、继续编辑和延迟交付。

### `send_generated_images`

发送或重发一个或多个 `genimg:` 原图，不创建新图片 API 请求。

### `list_image_capabilities`

返回两张模型卡、画幅、引用限制和交付模式。只用于能力询问。

## 画幅与尺寸

- AUTO_MAX：`landscape`、`portrait`、`square`、`photo`、`wide` 或 `W:H`，按当前模型卡最大像素计算。
- USER_FIXED：仅在用户明确指定时传 `WIDTHxHEIGHT`；无法满足就报错或跳过该卡，不静默缩小。
- Seedream 5 Pro 每次只输出一张图，最多接受 10 张参考图。
- WebP/GIF 参考图会在本地验证后转换为 PNG，再提交给只接受 JPEG/PNG 参考图的 Seedream 5 Pro。

## 配置

WebUI 中显示两张嵌套模型卡：

| 模型卡 | 默认地址 | 模型 |
| --- | --- | --- |
| 主模型卡 | `https://ark.cn-beijing.volces.com/api/v3` | `doubao-seedream-5-0-pro-260628` |
| Plan 回退卡 | `https://ark.cn-beijing.volces.com/api/plan/v3` | `doubao-seedream-5-0-pro-260628` |

Plan Key 留空时复用主卡 Key。两个 Key 字段都使用 AstrBot 的 `secret` 遮罩。

默认图片总预算为 110 秒，低于 AstrBot 常见的 120 秒 Tool 超时。若你修改 AstrBot 的 `tool_call_timeout`，可以相应调整本插件预算。

## 闭环状态

- `ok`：图片生成完成，按 `delivery` 判断是否已经交付。
- `generated_but_delivery_failed`：图片和 `genimg:` 已保存，但自动发送失败；Agent 可补发。
- `fail`：生成前校验、API、下载或存储失败，不能向用户声称成功。

`genimg:` 按平台、机器人和群聊/私聊 scope 隔离。插件不读取其他插件数据库，只消费工具返回的公开图片内容。

## 安装

要求 AstrBot `>=4.26.1`。安装仓库、填写主模型卡 API Key 并重载插件即可：

```text
https://github.com/zjj1280637679-ship-it/astrbot_plugin_yangmo_image_generation
```

## 许可

GNU Affero General Public License v3.0 或更高版本，见 `LICENSE`。
