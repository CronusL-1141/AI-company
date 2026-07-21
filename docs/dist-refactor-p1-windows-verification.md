# 分发改造 P1 — Windows 设备验证清单

> 用途:本机(macOS)无 Windows 环境,P1 的 Windows 兼容改动需在你的 Windows 设备上实测回报。
> 下面命令按顺序执行,每步给出**期望结果**。有偏差请把该步完整输出回贴。
> 涉及路径请按实际用户名替换 `<你>`;`~` 在 Git Bash 下即 `C:\Users\<你>`。

改动摘要(本清单验证的对象):
- hooks.json 的 auto_install 那条改成跨平台 launcher(Windows→`py -3`,macOS→`python3`),解自举死锁;超时 30s→300s。
- auto_install 版本自愈 + 首启进度卡 + 主链注册(绝对 `sys.executable` 路径,Windows 可 launch)。
- install.py/update.py/uninstall.py 补 skills/commands/agents(plugin 全量 25)分发与对称清理。

---

## 0. 前置环境勘察(先确认解释器与 shell)

PowerShell:
```powershell
py -3.12 --version        # 期望: Python 3.12.x（标准安装,稳定位)
py -3 --version           # 期望: 3.11+ 的某个版本(auto_install 用 py -3)
python --version          # 记录结果(可能指向易失的临时 3.13,仅作参考)
where.exe py python python3
```
期望:`py -3.12` 与 `py -3` 都能打印 3.11+ 版本。若 `py` 不存在,请先用 python.org 安装器装 Python(勾选 py launcher)。

Git Bash(开始菜单搜 "Git Bash";CC 在 Windows 默认用它跑 hook command):
```bash
uname -s                  # 期望: MINGW64_NT-10.0-... (证明 launcher 会走 py -3 分支)
py -3 --version           # 期望: 3.11+
```
若 `uname` 提示找不到,说明未装 Git Bash——请安装 Git for Windows(否则 CC 会退回 PowerShell,launcher 的 bash 语法不生效,这是已知文档级限制)。

---

## 1. 源码安装路径(你的迁移主路径:独立 clone → py -3.12 install → 卸插件)

在一个**全新目录**(不要与旧 checkout 共用)克隆并安装:
```powershell
cd <某个干净目录>
git clone https://github.com/CronusL-1141/AI-company.git ai-team-os
cd ai-team-os
py -3.12 install.py
```

### 1a. 安装尾部的 verify 报告
期望在安装输出末尾看到(全 `[OK]`,新增三类资产现在照得到):
```
  [OK] ~/.claude/agents/ templates (25 present)
  [OK] ~/.claude/skills/ (6/6 present)
  [OK] ~/.claude/commands/ (8/8 present)
  [OK] ~/.claude/settings.json hooks
  [OK] Python package (aiteam)
```
若 skills/commands 不是 6/6、8/8,回贴整段 verify 输出。

### 1b. 资产落位(PowerShell)
```powershell
(Get-ChildItem "$HOME\.claude\agents\*.md").Count          # 期望: 25(含 debate-advocate/debate-critic/team-member)
Get-ChildItem "$HOME\.claude\skills" -Directory | Select-Object -Expand Name
#   期望: autopilot, continuous-mode, meeting-facilitate, meeting-participate, os-register, os-workflow
Test-Path "$HOME\.claude\skills\meeting-facilitate\templates"   # 期望: True(嵌套子目录也拷到了)
(Get-ChildItem "$HOME\.claude\commands\os-*.md").Count      # 期望: 8
```

### 1c. 主链是绝对路径(Windows 可 launch 的关键)
```powershell
Select-String -Path "$HOME\.claude\settings.json" -Pattern "ai-team-os" | Select-Object -First 3
```
期望:命令形如 `"C:/Users/<你>/AppData/.../python.exe" "C:/Users/<你>/.claude/hooks/ai-team-os/send_event.py" ...`——
即**绝对 python.exe 路径 + 正斜杠**,而非裸 `python3`。这是 Windows 下 hook 能被 Git Bash 拉起的核心。

### 1d. 重启 CC 后功能核验
重启 Claude Code,在任一会话:
```
/mcp                      # 期望: ai-team-os 工具已挂载(155 个)
/os-status                # 期望: 命令存在且可执行(证明 commands 分发生效)
```
再确认 hooks 无报错:CC 会话顶部/transcript 不应出现 `... hook error`(尤其不应有 `python3 ... not found` / `exit 49`)。

---

## 2. 插件路径验证(可选,为其他 plugin 用户;若你只走源码安装可跳过)

若通过 marketplace 装插件(未跑 install.py):首次启动会话时,auto_install 经 launcher 被 `py -3` 拉起。

### 2a. 首启进度卡
期望首个会话顶部出现类似(中文进度卡):
```
[AI Team OS] 安装状态:
  ✓ 依赖包已安装（vX.Y.Z）   或   ✓ 依赖包已升级 → vX.Y.Z
  ✓ 主链已注册（N 个 hook，绝对路径）
  ✓ MCP 服务已配置
  → 重启 Claude Code 以解锁全部工具（一次性）
```
若依赖装失败,卡片应明说原因(Python 3.11+ 要求 / 网络失败)并给出可复制的 `pip install --upgrade git+...` 重试指令——**且会话不被阻塞**。

### 2b. 自愈后 hooks.json 被改写为绝对路径
auto_install 首次跑通后(_self_heal_interpreter 生效),查插件的 hooks.json:
```bash
# <PLUGIN_ROOT> 一般在 ~/.claude/plugins/... 下的 ai-team-os/plugin
grep -c "python3 " "<PLUGIN_ROOT>/hooks/hooks.json"    # 期望: 0 或很少(除 launcher 外都被改成绝对 python.exe)
grep -c "uname" "<PLUGIN_ROOT>/hooks/hooks.json"       # 期望: 1(auto_install launcher 那条被保留、未被改写)
```

### 2c. 重启后单运行时(无双跑、无噪音)
重启后再开一会话:进度卡应**零输出**(已就绪不加噪音);`/mcp` 工具在;transcript 无 `hook error`。

---

## 3. 卸载对称性(可选)

```powershell
py -3.12 scripts\uninstall.py --dry-run
```
期望 dry-run 列出:`[REMOVE] 25 agent template(s)` / `[REMOVE] 6 skill(s)` / `[REMOVE] 8 command(s)`,
且**不会**动你自己的第三方 skill/command(如 lark-* 等同目录资源)。

---

## 回报要点(把这几项结果发回即可)

1. 第 0 步:`uname -s` 输出、`py -3 --version`。
2. 第 1a 步:verify 的三类资产是否 `[OK]` 6/6、8/8、25。
3. 第 1c 步:settings.json 里 ai-team-os 命令是否为绝对 python.exe 路径。
4. 第 1d 步:重启后 `/mcp`、`/os-status` 是否可用,transcript 有无 `hook error`。
5. (若测插件)第 2a 步进度卡文案、第 2c 步重启后是否零噪音。
