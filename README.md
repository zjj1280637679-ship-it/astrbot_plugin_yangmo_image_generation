# AI 图片生成与原图交付

让 AstrBot 主 Agent 直接完成图片生成、参考图编辑、合成、内部预览和原图发送。v0.2.0 起插件完全使用 AstrBot 原生 Skill 渐进披露，不再自建 `prepare`、一次性句柄或固定工具调用顺序。

交流与反馈：**QQ 群 916646029**

## 核心逻辑

AstrBot 原生 Skills 本身就是两阶段：

```text
阶段 1：Skill inventory
  只把 name + description + SKILL.md 路径提供给模型

阶段 2：命中任务后
  模型读取 skills/image-generation/SKILL.md
  再按任务需要读取少量 references/*.md
```

本插件不再在 Tool Result 里重复实现第二套“伪 Skill 激活器”。图片工具始终是普通 LLM Tool，AI 可以在任何合适的推理步骤直接调用：

```text
generate_image(...)
send_generated_images(...)
list_image_capabilities()
```

**Skill 是工作方法，不是权限门；Tool 是执行接口。**

## 工具

### `generate_image`

直接生成、编辑或合成图片。无需 `prepare_image_generation`，无需句柄，也没有固定前置步骤。

```text
prompt: 完整图片指令
refs: [] | ["current", "genimg:..."]
count: 1..15
aspect: landscape / portrait / square / photo / wide / W:H
```

返回：

- `genimg:` 稳定引用；
- 内部预览，供当前 AI 观察；
- 实际模型、调用次数、参考图解析结果等结构化信息。

调用该工具会产生真实外部图片 API 请求。

### `send_generated_images`

把已有 `genimg:` 对应的原文件发送到当前聊天。它可以独立调用；发送失败时可以重试同一引用，不需要重新生成。

### `list_image_capabilities`

只读能力查询。只有模型、画幅、参考图边界或输出数量不确定时才需要调用；它不是生成前置步骤。

## 原生 Skill

入口：

```text
skills/image-generation/SKILL.md
```

初始上下文只暴露该 Skill 的名称和描述。命中图片任务后，AstrBot 按原生 Skills 规则读取 `SKILL.md`；其中再直接引用专项资料：

- 场景与构图；
- 编辑与合成；
- 插画与角色；
- 人像摄影；
- 海报与文字排版；
- 产品广告；
- 信息图；
- 游戏 UI / 资产；
- 实时知识视觉化。

只读取当前任务真正需要的最少资料，不批量加载整个技能目录。

## AI 可以自由决定调用顺序

插件不强制：

```text
prepare → generate → send
```

而是允许：

```text
直接 generate

generate → 看预览 → send

generate → 看预览 → 再 generate 编辑 → send

已有 genimg → 直接 send

不确定能力 → list → generate
```

如果当前任务不需要把图片真正发送到聊天，AI 也可以生成后继续完成其他推理；插件不会用生命周期守卫阻止它。

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
4. 根据自己的服务开通情况检查模型名、普通接口和套餐接口地址。
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
- 内部预览会作为工具结果提供给当前主模型检查；如果主模型是云服务，预览可能发送给该模型提供商；
- 原图保存在 AstrBot 插件数据目录；
- `generate_image` 的内部预览不等于已经发送到聊天；
- `send_generated_images` 才执行原文件真实发送；
- 本插件不修改 AstrBot 人格、主对话历史或其他插件配置。

## v0.1.x 升级说明

v0.2.0 删除旧协议：

```text
prepare_image_generation
image_task_handle
自定义 knowledge manifest 激活
一次性 prepare/consume 状态
```

升级后，旧的 `prepare_image_generation` 工具不存在；主 Agent 应直接使用 `generate_image`。这属于有意的接口简化。

## 许可

GNU Affero General Public License v3.0 或更高版本，见 `LICENSE`。
