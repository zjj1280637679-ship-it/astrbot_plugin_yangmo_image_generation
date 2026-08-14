# AI 图片生成与原图交付

给 AstrBot 主 Agent 增加一层轻量图片 harness：图片 Skill 使用 AstrBot 原生渐进披露，图片 Tool 始终可直接调用；生成默认自动交付原图，但 Agent 可以按上下文延迟交付、比较候选、继续编辑、穿插语言或调用其他工具。

交流与反馈：**QQ 群 916646029**

## 设计原则

本插件不再自建第二套 Agent 工作流。

```text
AstrBot Agent
  ├─ 自己决定：是否说话、检索、分析、调用其他工具
  ├─ 自己决定：现在要不要生成图片
  ├─ 自己决定：快速直出还是先比较/迭代
  └─ 自己决定：是否延迟交付

Image Skill
  └─ 提供按需创作方法，不是权限门

Image Harness
  ├─ generate_image
  ├─ 默认自动交付
  ├─ auto_send=false 可延迟交付
  ├─ genimg: 稳定引用
  └─ preview 回到同一个 Agent loop
```

核心边界是：

**Agent 决定行为，Skill 提供经验，Tool 提供能力，Harness 承担机械副作用。**

插件不要求固定预告、固定总结、固定工具顺序或最终文本。

## AstrBot 原生 Skill 两阶段

AstrBot Skills 使用渐进披露：

```text
阶段 1：Skill inventory
  只提供 name + description + SKILL.md 路径

阶段 2：命中任务后
  读取 skills/image-generation/SKILL.md
  再按任务需要读取少量 references/*.md
```

本插件不在 Tool Result 里重复注入另一套“Skill 激活器”。入口 Skill 只是工作方法，工具不以 Skill 是否加载作为调用条件。

## 工具

### `generate_image`

直接生成、编辑或合成图片：

```text
prompt: 完整图片指令
refs: [] | ["current", "genimg:..."]
count: 1..15
aspect: landscape / portrait / square / photo / wide / W:H
auto_send: true | false，默认 true
```

默认 `auto_send=true`：生成成功后插件立即发送原文件，同时把 `genimg:` 与内部预览返回给当前 Agent。这样普通生图不需要模型再做一次机械发送调用。

如果任务需要先比较候选、内部检查、继续编辑、择优交付或“先别发”，Agent 可以直接传 `auto_send=false`。这不是另一套生命周期，只是把交付时机留给 Agent。

每次 `generate_image` 都会执行真实外部图片 API，并可能产生费用。

### `send_generated_images`

发送或重发已有 `genimg:` 原文件。主要用于：

- `auto_send=false` 后择优交付；
- 自动发送部分失败后的补发；
- 用户要求重发；
- 稍后交付历史生成物。

它不是 `generate_image` 的必经下一步。

### `list_image_capabilities`

只读能力查询。用于确认模型、画幅、参考图边界、输出数量、交付策略或 prompt 指导；不是生成前置步骤。

## 上下文自适应，而不是模式枚举

插件没有 `fast_mode`、`slow_mode`、`quality_mode` 之类硬编码。主 Agent 直接从用户语境决定认知预算和工具轨迹。

### 快速路径

用户：

```text
立刻画一个苹果，越快越好。
```

理想行为：

```text
Agent
  → 不做无价值预告/查询
  → generate_image(..., auto_send=true)
Harness
  → 调 API
  → 保存
  → 自动发原图
  → preview + genimg 回 Agent
Agent
  → 可直接结束
```

用户最终可以只看到图片。

### Agentic 路径

用户：

```text
先认真构思，最多尝试三次，先别发候选，选最好的一张交付，最后锐评一下。
```

理想行为：

```text
Agent 预告（因为用户要求）
  → 读取必要 Skill/reference
  → generate_image(auto_send=false)
  → 看 preview
  → 必要时继续第 2/3 次
  → 选 best genimg
  → send_generated_images(best)
  → 根据 preview 继续锐评
```

“最多三次”属于本次任务预算，由 AstrBot Agent 自己遵守；插件不会把它固化为全局重试规则。

## Prompt 策略

插件不把“提示词越长越好”当成默认假设。

当前方舟公开资料更强调连贯自然语言、明确主体/行为/环境/用途和美学约束；部分 Seedream 指南还明确提醒，提示词过长可能让信息分散。因此本插件采用：

```text
高语义密度 > 机械堆字数
```

`list_image_capabilities` 会报告当前没有可靠的模型级硬 prompt 上限，并明确 `do_not_pad_to_percentage=true`。如果用户要求“写到上限 80%”，在没有可信硬上限时，Agent 应理解为充分展开有效视觉约束，而不是用同义反复填满假定长度。

## Agent 自由

插件同时保留两种自由。

**积极自由：** Agent 可以自由组合语言、图片、检索、分析和其他工具；可以连续编辑，也可以先内部比较再交付。

**消极自由：** 插件不强迫 Agent 预告、总结、固定顺序、最终文本、固定尝试次数，也不要求 Skill 激活以后才能调用图片工具。

默认自动发图只是 harness 默认副作用：当 Agent 已经决定生成时，插件替它完成最常见的机械交付。

## 原生 Skill 资料

入口：

```text
skills/image-generation/SKILL.md
```

按需引用：

- 场景与构图；
- 编辑与合成；
- 插画与角色；
- 人像摄影；
- 海报与文字排版；
- 产品广告；
- 信息图；
- 游戏 UI / 资产；
- 实时知识视觉化。

只读取当前任务真正需要的最少资料，不批量加载整个目录。

## 参考图

- `current`：当前用户消息中的图片；
- `genimg:`：本插件在当前会话作用域保存的历史生成物；
- 参考图按内容去重；
- 最多 14 张；
- 单张最多 30 MiB；
- 宽和高都必须大于 14 像素；
- 比例须在 `1:16..16:1` 内；
- 总像素不超过 3600 万；
- 5 Pro 候选最多接收 10 张，超过时会选择后续兼容模型。

其他插件提取码、QQ `file_id` 或临时 ID 不属于本插件引用域。

## 模型降级

- 默认按配置中的固定模型优先级尝试；
- 每个候选模型最多调用一次；
- 只有零结果且属于额度/限流类错误时才尝试下一候选；
- 一旦得到任何成功图片就停止降级；
- 不循环补画来凑足图片数量；
- 成功和失败回执都报告插件实际发起的 `api_calls`。

## 安装

1. 在 AstrBot 插件管理中使用仓库 URL 安装，或上传 ZIP。
2. AstrBot 版本需满足 `>=4.26.1`。
3. 在插件配置中填写火山方舟 API Key。
4. 根据服务开通情况检查模型名、普通接口和套餐接口地址。
5. 保存配置并重载插件。

仓库：

```text
https://github.com/zjj1280637679-ship-it/astrbot_plugin_yangmo_image_generation
```

## 配置

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `ark_api_key` | 空 | 方舟普通图片 API 密钥 |
| `ark_base_url` | 方舟北京普通接口 | 普通接口地址 |
| `image_models` | 5 Pro → 5 → 4.5 | 普通接口固定模型优先级 |
| `image_model` | 5 Pro | 模型列表为空时的兜底模型 |
| `image_size` | `5461x3072` | 无法识别画幅时的兜底尺寸 |
| `ark_plan_fallback` | `true` | 普通接口额度不足时尝试套餐接口 |
| `ark_plan_base_url` | 方舟套餐接口 | 套餐接口地址 |
| `ark_plan_api_key` | 空 | 套餐密钥；留空复用普通密钥 |
| `ark_plan_image_model` | Seedream 5 Lite | 套餐兜底模型 |
| `ffmpeg_bin` | `ffmpeg` | 制作内部预览；失败不影响原图 |
| `generated_ttl_days` | `30` | 本插件生成物保留天数 |
| `max_store_bytes` | `2147483648` | 本插件生成物存储上限 |

## 隐私与交付

- API Key 只从 AstrBot 插件配置读取，不写入日志和工具回执；
- 参考图会按图片 API 要求提交给配置的服务商；
- 内部预览会作为工具结果提供给当前主模型；如果主模型是云服务，预览可能发送给该模型提供商；
- 原图保存在 AstrBot 插件数据目录；
- `generate_image` 默认会自动把成功产物作为原文件发送到当前聊天；
- `auto_send=false` 时只保存并返回内部预览/引用，不自动交付；
- 本插件不修改 AstrBot 人格、主对话历史或其他插件配置。

## 从 v0.1.x 升级

v0.2.x 已删除：

```text
prepare_image_generation
image_task_handle
自定义 knowledge manifest 激活
一次性 prepare/consume 状态
```

主 Agent 直接使用 `generate_image`。这是有意的接口简化。

## 许可

GNU Affero General Public License v3.0 或更高版本，见 `LICENSE`。
