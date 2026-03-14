# Context Monitor - UserPromptSubmit Hook
# Reads context usage from statusline's monitor file and warns Claude when > 80%
param()

$monitorFile = Join-Path $env:USERPROFILE ".claude\context-monitor.json"

if (Test-Path $monitorFile) {
    try {
        $data = Get-Content $monitorFile -Raw -ErrorAction Stop | ConvertFrom-Json

        if ($data.used_percentage -ge 90) {
            Write-Output "[CONTEXT CRITICAL] 上下文使用率: $($data.used_percentage)%. 立即停止当前工作，保存所有记忆和进度到memory文件，然后提醒用户执行 /compact。不要开始任何新任务。"
        }
        elseif ($data.used_percentage -ge 80) {
            Write-Output "[CONTEXT WARNING] 上下文使用率: $($data.used_percentage)%. 请尽快完成当前节点任务，然后保存记忆和进度到memory文件，并提醒用户执行 /compact。"
        }
    } catch {
        # Silently ignore read errors
    }
}
