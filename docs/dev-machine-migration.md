# 开发机迁移指南（Windows → Mac / VS Code）

把 AI Team OS 的开发环境从 Windows 迁到 Mac。代码本身全跨平台，**没有版本兼容问题**；
要处理的只是「不随 git 走」的三样东西：未推送的提交、数据库、本机专属配置。

## 跨平台现状（已核实）

| 项 | 状态 |
|---|---|
| Python | 要求 `>=3.11`（`pyproject.toml`），Mac 装 3.11+ 即可 |
| Plugin hooks | 全用 `${CLAUDE_PLUGIN_ROOT}` + `python`，完全可移植 |
| API 自启动 `_autostart.py` | 已分别处理 win32 / unix（端口探测用 netstat/taskkill ↔ fuser/lsof/kill） |
| `.mcp.json` 生成 | `install.py` 用 `sys.executable` 写入，Mac 上自动写 Mac 的 python 路径 |

## 不随 git 走、必须手动处理的三样

1. **未推送的提交** —— 换机前在旧机 `git push origin master`（否则 Mac clone 缺最新代码）。
2. **数据库 `aiteam.db`** —— 在 OS 数据目录里（`aiteam paths` 的 `data_dir.database`，**不在仓库**）。
   要带现有团队 / 项目 / 任务数据，必须手动拷到新机的数据目录。
3. **本机专属配置** —— `.mcp.json` 和 `.claude/settings.json` 都被 `.gitignore`，不随仓库：
   - `.mcp.json`：`install.py` 在 Mac 会重新生成（正确的 Mac python 路径），无需手动。
   - `.claude/settings.json`：里面那个 `pwsh + inject-context.ps1` 钩子是 **Windows 专属**，
     Mac 上要么不用，要么换成 bash/python 等价实现。

## Mac 上的步骤

```bash
# 0)（在旧 Windows 机）先推送，确保 GitHub 有最新代码
git push origin master

# 1)（Mac）克隆并安装 Python 包（需 Python 3.11+）
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company          # 仓库目录
pip install -e .

# 2) 安装前端依赖并构建
cd dashboard && npm install && npm run build && cd ..

# 3) 注册 MCP + hooks（自动生成 Mac 版 .mcp.json）
python install.py

# 4)（可选，要保留现有数据）从旧机拷数据库到 Mac
#    旧机文件：旧机上 aiteam paths 输出的 data_dir.database
#    新机的数据目录由第 3 步的 install.py 建好，同样用 aiteam paths 查；拷之前先停掉新机的 API
cp /path/to/aiteam.db "<新机 aiteam paths 输出的 data_dir.path>/aiteam.db"

# 5)（可选，要用 Playwright 截图/E2E）
pip install playwright && playwright install
```

VS Code：装 Python 扩展即可，无其它特殊要求。

## 注意

- **换行符**：仓库走 autocrlf，Mac 用 LF，无功能影响。
- **`dashboard/dist`**：构建产物，Mac 上需重新 `npm run build`（`.gitignore` 已忽略其 JS/CSS）。
- **数据隔离**：不拷 `aiteam.db` 的话，Mac 是一套全新空库（团队/项目/任务从零开始）。
