# PreCompact Hook - Safety net for context preservation
# Fires when auto-compact or manual /compact triggers, records the event
param()

[Console]::InputEncoding  = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

try {
    $inputData = [Console]::In.ReadToEnd()
    $data = @{ trigger = "unknown"; timestamp = (Get-Date -Format "o") }

    if ($inputData -and $inputData.Trim().Length -gt 0) {
        try {
            $parsed = $inputData | ConvertFrom-Json -ErrorAction Stop
            $data.trigger = if ($parsed.trigger) { $parsed.trigger } else { "unknown" }
            $data.transcript_path = if ($parsed.transcript_path) { $parsed.transcript_path } else { "" }
            $data.session_id = if ($parsed.session_id) { $parsed.session_id } else { "" }
        } catch { }
    }

    # Write compact event log
    $logPath = Join-Path $env:USERPROFILE ".claude\compact-events.jsonl"
    ($data | ConvertTo-Json -Compress) | Add-Content -Path $logPath -Encoding UTF8

} catch {
    # Silently ignore errors - never block compact
}
