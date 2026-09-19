# Codex API 等值价格目录使用说明

状态：开发候选。价格目录、独立 API/CLI 不依赖账号登录，不读取订阅凭据，不采集会话，不写 OS 数据库。已有 Token 归因页面和 Claude 路径保持不变。

## 这次解决什么

计费工具内置旧表缺模型，不等于模型没有价格。本模块维护经过官方来源核验的完整目录，不依赖 ccusage 的缺价零值。随包提供当前 20 款模型、80 条模型/档位/长度费率和 10 个精确别名；具体来源与限制见 `src/aiteam/data/README.pricing.md`。

目录金额是 USD/百万 Token，以十进制字符串保存；计算全程 Decimal。每条价格保留官方链接与实际核验时间，未知生效区间为 null，不把核验日当作历史价格生效日。每次报价都带目录版本与内容 SHA256；要复算相同口径，应保留当时目录文件并固定 `--catalog`。

Spark 官方仍未公布 API 美元价，不能借 GPT-5.3-Codex 或 Fast 价格代填。它是明确记录的例外，不是免费；已公布价格的模型必须补齐。

## 输入与结果

例子中的 Standard 是显式选定的估值口径；自动接入时应使用响应记录的实际服务档位，不能用请求想要的 Fast 代替服务实际返回的 Default。

```json
{"requests":[{"request_id":"example-1","model":"gpt-6-astra","service_tier":"standard","input_tokens":100,"cached_input_tokens":60,"cache_write_input_tokens":10,"output_tokens":20}]}
```

- `input_tokens` 包含缓存读取与缓存写入；普通输入 = 总输入 - 缓存读 - 缓存写，三部分互斥。
- 输出已含 reasoning 子集，不重复收费。整份会话累计输入不能当作单请求长度来选价格档。
- `service_tier` 必填；`default` 对应 Standard，`priority` 对应 Fast。缓存默认 0 只适用于调用方确认没有该类输入的请求，不能把未采集当作测得 0。
- 不接受重复 request_id、负数、浮点 Token、布尔 Token、缺少模型/档位或缓存总量超出输入；非法批次整体拒绝，不悄悄丢弃请求。
- 未覆盖模型/档位/输入区间保留在结果中。`request_count` 是全部合法提交请求的分母；`priced_request_count` 是已报价分子，不是账号采集覆盖率或 Token 覆盖率。
- 完整时 `total_usd` 是这批请求的 API 等值；不完整时总额为 null，`priced_subtotal_usd` 只表示已知小计。缺价不会缓存成永久零值。

## 使用

以下命令在已安装此候选包的环境使用；从源码测试可加 `PYTHONPATH=src`，将 `aiteam` 换成 `python3 -m aiteam.cli.app`。

```sh
aiteam pricing catalog
aiteam pricing validate --catalog verified-catalog.json
aiteam pricing quote --input requests.json --catalog verified-catalog.json
aiteam pricing supplement --catalog old-catalog.json --supplement verified-additions.json --version openai-new-revision
```

`supplement` 的输入使用同一完整目录契约，但 records 可以只包含本次已核验的补价项。按 `(model, tier, min_input_tokens, max_input_tokens)` 精确合并：同键更新、其他键保留；仍会拒绝区间重叠、旧核验时间回退、改写已有别名路由和把已有报价模型改成未价。改变价格区间分段或有官方依据的别名路由时应提供新的完整目录，不能用补价命令模糊覆盖。

这些命令只向 stdout 输出 JSON，不会覆盖原目录、安装配置或重启服务。`quote` 退出码 0=完整、2=缺价、1=输入/目录无效。补价后用相同 requests.json 指向新目录再算，即可看到被补齐的模型与新的版本摘要。

API：`GET /api/pricing/catalog` 返回有效目录与 SHA；`POST /api/pricing/quote` 接受上例并重新计算，不保存提交数据。默认读取包内目录；部署者可显式配置 `AITEAM_PRICING_CATALOG` 指向已校验快照。客户端不能通过 API 指定服务端任意文件路径。目录错误返回 503，不回传路径或错误原始内容。

## 边界

这是“按指定价格版本估值”，不是实际支付、ChatGPT credits、订阅扣款或历史账单。GPT-5.5/5.4 的 full-session 计费语义、数据驻留、工具收费、税费与合同折扣均不能由逐请求 Token 自动还原。未完成账号绑定/同期周额度采样前，不提供全账号周费用外推。

价格服务不自动联网抓价、不内置价格刷新定时器。补价来源先核验，目录独立版本化；不能把第三方新抓到的报价无审核覆盖官方目录。Dashboard 账号展示消费独立结果，不能把金额塞回已有 Token 归因字段。账号额度的用户可选监控另见 [账号使用说明](USAGE.account-usage.md)，它不会自动确认请求归属或金额覆盖。

## 规则审查

Council ④ 有限审查记录：`5b696e2d-3103-4eea-9e7f-7492069c4aa9`。只为具名 Pricing 模型建立独立量纲契约，旧四量纲、旧页面以及未登记字段/重导出仍受拦截。没有修改 Claude 的指令、配置、Hook 或授信。
