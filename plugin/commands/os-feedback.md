---
name: os-feedback
description: 把对 AI Team OS 的 bug 或建议发给 OS 维护者（GitHub Issue 或 Discussions 的 Ideas），发出前用户看原文并确认
---

# /os-feedback — 向 OS 维护者反馈

## 用法

- `/os-feedback <一句话>` — 判定类别，起草，预览，查重，用户确认后提交。
- `/os-feedback` — 调 `feedback_list()`，列最近的反馈台账（类别、标题、状态、编号、链接）。

## 先判定是什么

只收关于 AI Team OS 本身的反馈（它的 hook、MCP 工具、API、Dashboard、安装器）。关于
Claude Code、Codex 或模型本身的，请用户走各自的渠道。

| 情形 | 怎么办 |
|---|---|
| 行为与文档或工具说明不符、有报错、能说出复现步骤 | `kind="bug"`，发成 Issue |
| 想要现在没有的能力，或想改变按设计工作的行为 | `kind="idea"`，发到 Discussions 的 Ideas |
| 安全漏洞（绕过校验、越权、泄露） | `kind="security"`：不公开发，只返回私密报告链接 |
| 使用疑问 | `kind="question"`：返回 Q&A 链接 |
| 「这里很难用」这类两可的话 | **问用户**是 bug 还是建议，不替用户定 |
| 一段话里既有 bug 又有建议 | 拆成两份草稿 |

撞上 OS 报错时可以**提议**反馈，但只有用户说要反馈之后才起草。

## 起草

1. 起草前用 `memory_search` 查方向层里「私有术语」一类的防泄记忆，对照草稿自查，确保
   这些词不进正文；在预览里写一句自查结论。
2. 标题与正文用英文写；用户不是用英文说的，把用户原话原样放进 `original`，并填
   `original_language`。用户明说「只发我的原话」时不翻译，把原话放进 `body`。
3. 调 `feedback_draft(...)`：bug 填 `title`、`body`（发生了什么）、`steps`（复现步骤）；
   idea 填 `title`、`body`（问题）、可选 `proposal`、`alternatives`；补充到已有帖子用
   `comment_on="issue:12"` 或 `"discussion:5"`，只填 `body`。
4. 返回 `status="refused"`：命中疑似密钥或本机私有术语词表。按 `refusal` 里的字段与行号
   请用户改写后重新起草，没有放行的办法。
5. 返回 `status="duplicate"`：同样的内容已经发过，把链接告诉用户，不再新建。

## 预览（三件事缺一不可）

- **逐字展示**返回的 `title` 与 `body`，不概括、不改写，并给出 `content_hash` 前 12 位。
  `rewrites` 与 `notes` 里的改写与提醒（未替换的登录名、可能的私有名称、残留路径）也
  告诉用户。
- **相似条目**（`similar` 与 `similar_sent_here`）列给用户，请用户三选一：新开；补充到
  其中一条（重新起草，带 `comment_on`）；不发。
- 改了任何一个字都要重新 `feedback_draft`，旧草稿作废。

## 提交

调 `feedback_submit(draft_id)`。客户端会弹出确认框：先列去处、标题、内容哈希和一个只读的
全文页链接，再列原文，最后一行说明「Accept = 立即公开发布 / Decline = 不发」。框里没有
别的选项，用户点 Accept 就立即发出（默认焦点在 Accept，回车即发），点 Decline 什么都不发；
你不能替用户回答。调用之前先提醒用户这一点。

- 发出后把 `remote_url` 告诉用户。
- 返回 `outcome="declined"`（用户在弹框里拒绝了）：什么都没发出，草稿保留到过期；照实
  告诉用户，不要追问着重发。
- 返回 `handed_off`：客户端弹不了框、弹框被取消或 5 分钟无人应答、或 GitHub 授权没有
  完成。把 `url` 给用户，由用户在 GitHub 页面上检查后亲手提交；有 `file` 时说明按文件里
  各小节逐项粘贴。
- 返回 `unknown`：发送超时，帖子可能已经发出。不要重发，稍后用 `feedback_list` 查看。
- 本机 24 小时内最多发 5 条；超限时把原因告诉用户，不重试。

## 用谁的身份发

先用用户本机已登录的 gh；gh 不可用时，用用户此前授权给 OS 的 GitHub App（「AI Team OS
Feedback」）。两样都没有、而客户端能弹框时，草稿的 `route` 是 `auth`：起草时查不了重
（`search_failed`），如实告诉用户，请用户自己到仓库里看一眼。

`route="auth"` 的草稿提交时，用户先在发布确认框里点 Accept，随后弹出第二个框：第一行是
授权码，下面是 `https://github.com/login/device`。用户点 Accept 后在该页输入授权码并同意，
帖子随即以用户本人的 GitHub 账号发出；点 Decline 返回 `outcome="auth_declined"`，什么都
不发。授权只覆盖反馈仓库的 issue 与讨论。

- 返回 `status="authorizing"`：用户还没在 GitHub 上完成授权。把 `user_code` 与
  `verification_uri` 告诉用户；用户说完成了，再调一次 `feedback_submit(draft_id)`，
  不会再问是否发布，授权码也不变。
- 返回 `outcome="auth_denied"`：用户在 GitHub 上拒绝了授权，什么都没发出。
- `feedback_account()` 查看现在走哪条路（`gh`、`app` 或 `none`）、以谁的身份、App 授权
  何时到期；不会显示令牌。用户要撤销时调 `feedback_account(action="sign_out")`：删掉本机
  保存的授权，并把返回的 `revoke_page` 给用户，在 GitHub 上一并撤销。

## 工具不可用时

`feedback_*` 工具不在 default 工具组里。用户把 `AITEAM_TOOLSETS` 裁到 default 时没有这组
工具：请用户在 `AITEAM_TOOLSETS` 里加上 `feedback` 并重启会话，或直接打开
`https://github.com/CronusL-1141/AI-company/issues/new/choose` 手动提交。手动提交没有 OS 的
脱敏，提醒用户自己检查路径、用户名、令牌与私有名称。

## 私有术语词表

机检用的词表是 OS 数据目录（`aiteam paths` 可查）下的 `feedback-private-terms.txt`：一行
一个词，`#` 开头为注释，最多 500 条；英文词按整词、不分大小写匹配，中文词按出现即匹配。
文件由用户自己维护，不随仓库分发，也不会发往 GitHub；没有这个文件时这一层为空。
