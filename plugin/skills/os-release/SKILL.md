---
name: os-release
description: 发布 AI Team OS 新版本的完整清单——预检、版本七处锁步、中英双语 CHANGELOG、双份 dist 构建、私有术语扫描、commit/tag、双仓推送、建 GitHub Release 条目并核对 latest 徽章、事后核对。当准备发版、补建漏掉的 Release 条目、或核对已发版本的线上状态时使用。
---

# OS Release — 发版清单

## 为什么有这份清单

凡是进了 `scripts/check_invariants.sh` 的红线都没烂；没进机检的漏了两次，且完全同型：

- v1.10.0/1/2 的 tag 是 2026-07-14 打的，GitHub Release 条目 07-21 才批量补；
- v1.10.3/v1.11.0/v1.11.1 的 tag 分别是 07-27 / 07-27 / 07-29 推的，Release 条目
  07-30 才一次补齐（实测三条 `publishedAt` 挤在 30 秒内），期间访客主页侧边栏两周
  显示 `Latest v1.10.2`。

机检管得住的部分不用你记，跑一条命令即可。这份清单只管**机检管不住的部分**，以及
**谁按哪个按钮**。

## 执行分工（硬约束：发布流水线必须可中断）

| 步骤 | 谁执行 |
| --- | --- |
| 0–6 准备与校验 | Leader 全权 |
| 7 commit · 8 tag | Leader 执行，但**先把 `git diff --stat` 与拟好的 message 交用户批准** |
| 9 双仓推送 · 10 建 Release 条目 | **用户执行**。Leader 只把命令准备好打印出来 |
| 11 事后核对 | Leader（只读） |

不要代替用户 push / publish，也不要在未批准前 commit。

---

## 0. 确认在哪儿干活

```bash
git branch --show-current
```

本仓库并行会话纪律：第二个及之后的会话必须用 `git worktree` 隔离。发版这类跨全树的
改动尤其不能与别人共享 checkout。

## 1. 预检全绿

```bash
bash scripts/preflight.sh          # 发版必须跑全量，不要 --fast
```

四道门禁：`ruff check src/ tests/` → `dashboard npm run lint` → `check_invariants.sh`
（I1–I14）→ `pytest tests/unit/`。

失败长这样：每项后面跟 `✗ <门禁名> 失败`，末尾 `✗ 预检未通过 — 修复后再 push`。
退出码非 0。

一处**既定豁免**：未构建 `dashboard/dist` 的环境里 I3 输出
`⚠️ dist 目录缺失（未构建环境可忽略）`——警告不拦。但发版必须构建（见第 4 步），
所以发版时这条不该还是警告。

## 2. 版本锁步七处

**I2 机检的五处**（漂移即红，`❌ [I2] 版本号漂移: ...`）：

- `pyproject.toml`
- `src/aiteam/__init__.py`
- `plugin/.claude-plugin/plugin.json`
- `plugin/.claude-plugin/marketplace.json`
- `.claude-plugin/marketplace.json`

**另两处由 I6 第①类硬等式盯**：`README.md` 与 `README.zh-CN.md` 的 announcement 行
（`> ⚡` 开头）必须含 `v<新版本>`，否则 `❌ [I6] README 数字漂移`。

**机检不覆盖、必须人眼看的**：`plugin/.claude-plugin/plugin.json` 里 `description`
文本中的数字（MCP 工具数、生命周期事件数）。v1.11.1 那次人审抓到的唯一实锤就在这里
——两处数字腐烂了整整一个版本。

README 里的其余数字（工具数/页面数/端点数/测试数）由 I6 对照实测校验，**按实测改，
不要按记忆改**。

## 3. CHANGELOG 中英双语同步

- 英文 `CHANGELOG.md` 加新版本段，标题格式 `## [x.y.z] - YYYY-MM-DD`。
- 中文 `CHANGELOG.zh-CN.md` **同步全译**。缔造者 2026-07-30 裁定：中文版维持**镜像
  全译契约**——不是摘要，也不退役。
- 段落内容按 `git log` 与设计文档**逐条取证**写成，不照抄计划（计划里没做成的东西
  写进 CHANGELOG 就是假账）。

这一步**没有机检**，是本清单里最容易再烂的一环：中文版曾从 1.9.0 起停更 6 个版本
（1.10.0 → 1.11.1），07-30 才回补。写完后自查一遍两个文件的版本段集合是否相同。

## 4. dashboard 双份 dist 构建一致（I3）

```bash
cd dashboard && npm run build && cd ..
rm -rf plugin/dashboard-dist
cp -R dashboard/dist plugin/dashboard-dist
```

I3 的失败提示写的是 `cp -R dashboard/dist plugin/dashboard-dist`；目标目录已存在时
这条会在里面套一层 `dist/`，所以**先 `rm -rf` 再 `cp`**。不删还有第二个坑：bundle
文件名带 hash，旧文件不会被覆盖，会作为过期产物留在分发包里。

失败长这样：`❌ [I3] dashboard/dist 与 plugin/dashboard-dist 的 JS bundle 不一致`。

`plugin/dashboard-dist/` 是 marketplace 用户拿到的那份产物——它必须与本批前端源码
**同一个 commit 产出**。

## 5. 私有术语关键词扫描（四个面）

**词表不写在这里**：把禁用词表写进即将发布的仓库，等于把要防的东西发布出去。执行前用
`memory_search` 拉方向层那条防泄记忆（检索词「私有术语防泄」）取当期词表。

四个面（历次发版实际扫过的就是这四个，`f86a63e` 与 `b92295b` 的 message 里有记录）：

```bash
PREV=v<上一个版本>
git log --format='%h %B' $PREV..HEAD      # ① 本批全部 commit message
git diff $PREV..HEAD                      # ② 本批触及的全部跟踪文件内容
git diff --name-status $PREV..HEAD        # ③ 本批新增/改名的文件路径本身
```

④ **即将写下的 release commit message 自身**——写完先扫一遍再提交。这一面最容易漏，
因为它在 git 里还不存在。

命中即停：树面脱敏后重来。实录（批 9）：一个 agent 把私有设计文档名与内部术语写进
`types.py`、`hook_translator.py`、测试注释和 commit message，靠人工扫描抓获，最终
需要用户授权重写历史才归零。发版扫描是**最后一道闸**，第一道闸在派工 prompt 里。

## 6. 开发版/分发版同步人审

`src/` 是开发版，`plugin/` 是分发版。机检已覆盖：I1（hook 双副本逐字节）、I3（双
dist）、I6（README 数字）、I8（hook 注册面 install.py ↔ hooks.json ↔ 双语 README）。

**机检覆盖之外，逐条人眼过**：

- `plugin/.mcp.json` 有没有版本引用
- `install.py` 有没有内嵌版本号
- `plugin/.claude-plugin/plugin.json` 的 `description` 文本里的数字（见第 2 步）
- `plugin/dashboard-dist/` 是否与前端源码同 commit 产出：bundle 里有没有本批新页面的
  代码、`index.html` 引用的 bundle 名与 `assets/` 里的实际文件名一致
- 新增了 skill / agent 模板 / commands？`install.py` 走目录遍历（`copy_skills` 等），
  加目录不需要改代码，但要确认目录名与 frontmatter 的 `name` 一致

## 7. commit（先交用户批准）

message 用中文，不附任何 agent 署名（禁止 `Co-Authored-By:` 之类）。照历次 release
commit 的结构写清：

1. 版本性质与号段理由（为什么是 patch / minor）
2. 版本七处锁步：旧版 → 新版
3. CHANGELOG 段的取证要点（Added / Fixed / Changed / Upgrade notes 各写了什么）
4. 开发版/分发版人审结论（第 6 步逐条的结果）
5. 关键词扫描四面的结论
6. 验收数字：pytest 通过/跳过数、`check_invariants.sh` I1–I14 结果（含既定豁免）

先把 `git diff --stat` 和拟好的 message 给用户看，批准后再 commit。

## 8. tag

```bash
git tag -a v<x.y.z> -m "<与 commit 标题同义的一行>"
```

历史上 annotated 与 lightweight 混用（v1.10.1/2/3 是 lightweight，v1.10.0/v1.11.0/
v1.11.1 是 annotated）。用 `-a`，与最近两版一致。

## 9. 双仓推送（**用户执行**）

```bash
git push origin master        # origin = 私有仓（日常推送）
git push origin v<x.y.z>
git push public master        # public = 公开仓（发版同步 + tag）
git push public v<x.y.z>
```

双仓策略：v1.8.0 起公开仓同步完整版。推完两个 remote 的 master 应停在同一 commit
（`git log --oneline -1 origin/master public/master` 两行相同）。

`--verify-tag`（下一步）校验的是**远端**有没有这个 tag，所以 tag 必须先推上去。

## 10. 建 GitHub Release 条目（**用户执行 publish**）

Leader 先生成正文与命令——离线、可重复、不联网：

```bash
python3 scripts/release_notes.py <x.y.z>
```

它做四件事：把 `CHANGELOG.md` 该版本段的**原样切片**写成 notes 文件、校验该段存在且
非空、校验版本与 I2 五处一致、校验本地有该 tag；然后打印可直接粘贴的
`gh release create ... --verify-tag --title ... --notes-file ...`。**脚本自己绝不
发布。**

用户执行打印出来的命令。标题惯例是 `v1.11.1 — <英文副标>`，副标要人写，脚本不编造，
用 `--title` 传：

```bash
python3 scripts/release_notes.py 1.11.1 --title "v1.11.1 — Truthful Ledgers"
```

**补建多个漏掉的条目时**：加 `--backfill`（显式声明版本号与当前树不一致是有意的），
并且**按版本升序逐个 create**。GitHub 的 latest 徽章看发布时间，倒序建会把旧版本顶成
latest——2026-07-30 那次正是按 1.10.3 → 1.11.0 → 1.11.1 升序补，latest 才落对。

## 11. 事后核对（Leader，只读）

```bash
python3 scripts/release_notes.py --check <x.y.z>
```

全绿长这样：

```
✅ 正文与线上逐字节一致（15597 字符）
ℹ️  线上标题: 'v1.11.1 — Truthful Ledgers: ...' · 发布时间 2026-07-30T04:21:30Z
✅ latest 徽章 = v1.11.1
```

失败长这样，以及各自意味着什么：

| 输出 | 含义 | 怎么办 |
| --- | --- | --- |
| `❌ <仓库> 上没有 v<x> 的 Release 条目` | tag 推了、条目没建——就是这两次的漏项 | 回第 10 步 |
| `❌ 正文与线上不一致` + `差异只在换行/空白` | 渲染结果无差别（v1.10.0/1/2 那批是 strip 过的切片，属历史差异） | 可不动；要对齐就用它打印的 `gh release edit` |
| `❌ 正文与线上不一致` + `仍不相等` | 条目建好后 CHANGELOG 又被改过 | 用打印的 `gh release edit` 覆盖正文（用户执行） |
| `❌ latest 徽章指向 <旧 tag>` | 访客主页侧边栏正显示旧版本 | 检查发布顺序，必要时重发最新那条 |
| `⚠️ --check 跳过：拿不到线上 Release` | 网络/鉴权/`gh` 缺失。脚本只报告不把关，退出码 0 | 换网络重跑，别当成通过 |

`--check` 的逐字节可比性只从 **v1.10.0** 起成立：v1.9.0 及更早的条目正文是手写的，
与 CHANGELOG 段实质不同（实测 v1.9.0 线上 1375 字符 vs CHANGELOG 段 4012 字符）。

顺手核对一遍公开仓侧边栏：`gh release list --repo <owner/name> --limit 5`。

---

## 为什么这一条没做成机检

`scripts/check_invariants.sh` 必须**离线确定性可跑**（CI 与本地通用、无外部依赖），
而查 Release 条目在不在、latest 徽章指向谁**必须联网**。把它塞进机检要先回答"机检
允不允许联网、联不上算红还是算跳过"——那本身是个需要先定的设计问题。加上本仓库纪律
（新增/修改机检红线必须先过会），所以这条留在按需脚本 `scripts/release_notes.py
--check` 里，由本清单第 11 步调用，而不是伪装成一条永远可能因为断网而变绿的机检。
