# SubagentStart Hook — 向子agent注入CLAUDE.md和OS注册指引
# stdout内容会作为additionalContext注入子agent上下文
param()

$projectRoot = "C:\Users\TUF\Desktop\AI团队框架\ai-team-os"
$claudeMd = Join-Path $projectRoot "CLAUDE.md"

$output = ""

# 注入CLAUDE.md核心内容
if (Test-Path $claudeMd) {
    $content = Get-Content $claudeMd -Raw -ErrorAction SilentlyContinue
    if ($content) {
        $output += "=== AI Team OS 项目指令 ===`n"
        $output += $content
        $output += "`n"
    }
}

# 注入OS注册指引
$output += @"

=== AI Team OS 自动注册指引 ===
你正在一个配置了AI Team OS的项目中工作。请在开始工作前：
1. 使用MCP tool: os_health_check 检查OS是否可用
2. 如果可用，使用MCP tool: team_list 查看现有团队
3. 使用MCP tool: agent_register 注册自己到团队
4. 注册后阅读返回的team_snapshot了解团队状态
如果OS不可用，跳过注册，正常执行你的任务。
"@

# 输出JSON格式（CC要求的additionalContext格式）
$jsonOutput = @{
    hookSpecificOutput = @{
        hookEventName = "SubagentStart"
        additionalContext = $output
    }
} | ConvertTo-Json -Depth 3 -Compress

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::Write($jsonOutput)
