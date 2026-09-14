# OpenAI API 费率目录

本目录是截至 **2026-09-14 04:55:51 UTC** 核验的 OpenAI 官方公开 API 费率快照。
`openai_pricing.json` 的版本为 `openai-2026-09-14`，涵盖 20 个有公开费率的模型、80 条模型/档位/输入区间记录、10 个有官方依据的精确别名，以及 1 个明确未价模型。

## 金额口径

所有单价均为 **USD / 1,000,000 tokens**，以十进制字符串保存。它们用于按选定目录估算模型 token 成本，不是实际账单，也不表示 ChatGPT 订阅额度、credits、API 实付或历史某日价格。

- `input` 是普通未缓存输入的单价；`cached_input` 是缓存读取单价；`cache_write` 是缓存写入单价；`output` 是输出单价。
- 输入 token 总量包含缓存读取及写入。普通输入量应为 `input_tokens - cached_tokens - cache_write_tokens`。三种输入费率互斥；缓存写入费率不是附加费。见[官方缓存计价公式](https://developers.openai.com/api/docs/guides/prompt-caching#monitor-cache-performance)。
- GPT-5.6 及之后的模型有单独写缓存费率。GPT-5.5 及更早模型的官方规则为没有额外写入收费，因此这些记录将 `cache_write` 设为与普通 `input` 相同的单价，并在 `notes` 说明；不以零值表示。见[官方模型缓存差异](https://developers.openai.com/api/docs/guides/prompt-caching#summary-of-model-differences)。
- `null` 表示未知或未提供该项价格，不等于零。当前 80 条记录的四类费率均有来源依据，未价模型单独列入 `unpriced_models`。
- 未知模型、未公开服务档位、超出目录输入区间、缺少计价所需 token 分类，不能由最近似模型或固定倍率补全。
- 未计入工具调用、文件/容器存储、区域数据驻留加价、税费、合同折扣、credits 或其它独立账单项目。官网所述适用模型区域处理加价为 10%，本目录保留普通 API 基础费率。

## 输入区间与服务档位

`min_input_tokens` 和 `max_input_tokens` 均为闭区间边界。短上下文为 0 至 272000，长上下文从 272001 开始；有长上下文价的请求整次采用匹配记录，不按越过阈值的部分累进计算。

Astra 与 GPT-5.6 模型页明确最大输入为 922000。GPT-5.3-Codex、GPT-5.2-Codex、GPT-5-Codex、GPT-5/mini/nano、GPT-5.4-mini/nano 页明确最大输入为 272000。其它模型页仅给上下文窗口时，目录以上下文窗口作为报价覆盖界，并在记录中注明这不是 API 最大可用输入承诺；未自行扣减最大输出量。

GPT-5.5 与 GPT-5.4 官方将 Standard/Batch/Flex 的长上下文倍率描述为 **full session**。本目录保留其公布的短/长费率，只用于当前所选 rate card 的估值；逐条使用量结果不能声称重建了 session 的最终账单。两款模型没有公开 Fast 长上下文价，故不存在该组合的记录。

API `fast` 与 `priority` 是同一服务档位的名称；实际计价应采用响应返回的档位。请求可能降级并返回 `default`，此时适用 Standard。目录采用规范值 `standard/fast/flex/batch`。见[Fast mode 文档](https://developers.openai.com/api/docs/guides/fast-mode)。

GPT-5.6 与 Astra 的 API Fast 单价是 Standard 的 2 倍；ChatGPT credits 的倍率不同，不能混用。见[Codex 速度与 API 口径说明](https://learn.chatgpt.com/docs/agent-configuration/speed)。
Astra Fast 不适用于 EU 数据驻留；本目录中存在费率不表示当前账户或地域具备使用资格。

## 来源与模型覆盖

当前和普通历史模型的多档位费率来自[官方 API 定价表](https://developers.openai.com/api/docs/pricing)。
下表的单价顺序为普通输入 / 缓存读取 / 缓存写入 / 输出，均为 Standard 短上下文 USD/百万 tokens。
各模型链接同时支持模型标识、快照及输入边界的核验。历史专用 Codex 模型的 Standard 价以各自模型页为来源；GPT-5.3-Codex Fast 价来自主定价表。

| 模型及官方模型页 | Standard 短上下文单价 | 已列服务档位 |
| --- | --- | --- |
| [gpt-6-astra](https://developers.openai.com/api/docs/models/gpt-6-astra) | 10.00 / 1.00 / 12.50 / 50.00 | standard / fast / flex / batch |
| [gpt-5.6-sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol) | 4.00 / 0.40 / 5.00 / 20.00 | standard / fast / flex / batch |
| [gpt-5.6-terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra) | 2.00 / 0.20 / 2.50 / 12.00 | standard / fast / flex / batch |
| [gpt-5.6-luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) | 0.20 / 0.02 / 0.25 / 1.20 | standard / fast / flex / batch |
| [gpt-5.5](https://developers.openai.com/api/docs/models/gpt-5.5) | 5.00 / 0.50 / 5.00 / 30.00 | standard / fast / flex / batch |
| [gpt-5.4](https://developers.openai.com/api/docs/models/gpt-5.4) | 2.50 / 0.25 / 2.50 / 15.00 | standard / fast / flex / batch |
| [gpt-5.4-mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini) | 0.75 / 0.075 / 0.75 / 4.50 | standard / fast / flex / batch |
| [gpt-5.4-nano](https://developers.openai.com/api/docs/models/gpt-5.4-nano) | 0.20 / 0.02 / 0.20 / 1.25 | standard / flex / batch |
| [gpt-5.2](https://developers.openai.com/api/docs/models/gpt-5.2) | 1.75 / 0.175 / 1.75 / 14.00 | standard / fast / flex / batch |
| [gpt-5.1](https://developers.openai.com/api/docs/models/gpt-5.1) | 1.25 / 0.125 / 1.25 / 10.00 | standard / fast / flex / batch |
| [gpt-5](https://developers.openai.com/api/docs/models/gpt-5) | 1.25 / 0.125 / 1.25 / 10.00 | standard / fast / flex / batch |
| [gpt-5-mini](https://developers.openai.com/api/docs/models/gpt-5-mini) | 0.25 / 0.025 / 0.25 / 2.00 | standard / fast / flex / batch |
| [gpt-5-nano](https://developers.openai.com/api/docs/models/gpt-5-nano) | 0.05 / 0.005 / 0.05 / 0.40 | standard / flex / batch |
| [gpt-5.3-codex](https://developers.openai.com/api/docs/models/gpt-5.3-codex) | 1.75 / 0.175 / 1.75 / 14 | standard / fast |
| [gpt-5.2-codex](https://developers.openai.com/api/docs/models/gpt-5.2-codex) | 1.75 / 0.175 / 1.75 / 14 | standard |
| [gpt-5.1-codex-max](https://developers.openai.com/api/docs/models/gpt-5.1-codex-max) | 1.25 / 0.125 / 1.25 / 10 | standard |
| [gpt-5.1-codex](https://developers.openai.com/api/docs/models/gpt-5.1-codex) | 1.25 / 0.125 / 1.25 / 10 | standard |
| [gpt-5.1-codex-mini](https://developers.openai.com/api/docs/models/gpt-5.1-codex-mini) | 0.25 / 0.025 / 0.25 / 2 | standard |
| [gpt-5-codex](https://developers.openai.com/api/docs/models/gpt-5-codex) | 1.25 / 0.125 / 1.25 / 10 | standard |
| [codex-mini-latest](https://developers.openai.com/api/docs/models/codex-mini-latest) | 1.5 / 0.375 / 1.5 / 6 | standard |

两处保留官网原始精度和限定：

- GPT-5.4 Batch/Flex 短上下文 `cached_input` 为官网显示的 `0.13`，未改为推算的 `0.125`。
- GPT-5.6 Sol 优惠价承诺至少持续至 2026-11-21；“至少持续”不能作为确认的结束日期，因此 `effective_until` 仍为 `null`。

`gpt-5.6` 路由至 Sol 的别名关系来自 [Sol 模型页](https://developers.openai.com/api/docs/models/gpt-5.6-sol)。
另外 9 个日期快照别名只采用对应模型页列出的精确字符串；不以任意前缀、日期后缀或模糊模型名匹配费率。

## 未公开价格

`gpt-5.3-codex-spark` 的[官方 Codex token 费率表](https://learn.chatgpt.com/docs/pricing#token-rates)仅标为研究预览，没有 API USD 单价。
[官方速度页](https://learn.chatgpt.com/docs/agent-configuration/speed#codex-spark)说明其是独立模型、使用独立限制，并面向 Pro 研究预览。
故目录将它列为未价，不能套用 GPT-5.3-Codex、快速模式或其它模型价格，更不能按免费处理。

## 时间与维护

`verified_at` 是实际核验公开页面的 UTC 时刻，顶层和每条记录都保留该字段。它既不是模型发布日期，也不是价格生效日期。
由于本次来源没有确立每项价格的完整生效区间，所有记录的 `effective_from/effective_until` 均为 `null`。
当前价格快照不能自动用于断言过去某次运行当时应付的价格。

新增或更新时应逐条核对主定价表、模型页、缓存和服务档位规则，保留原记录的核验时间；新的核验时间只标记实际重查过的记录。
保留已验证十进制价格、明确来源与区间，不根据模型名猜价；未知组合明确报告未覆盖。
运行环境不需要联网刷新或安装额外依赖才能读取此快照。
