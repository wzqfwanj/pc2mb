<#
.SYNOPSIS
  注册 / 卸载 pc2mb Windows Agent 的开机自启（当前用户登录时后台隐藏运行）。

.DESCRIPTION
  生成 agent\.launch.cmd（把 stdout/stderr 追加到 agent\agent.log）和 agent\.launch.vbs
  （隐藏窗口调用），再注册计划任务 pc2mb-agent（AtLogOn，失败自动重启，无执行时长限制）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\install-agent-autostart.ps1
  powershell -ExecutionPolicy Bypass -File deploy\install-agent-autostart.ps1 -Uninstall
#>
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'

$taskName  = 'pc2mb-agent'
$repo      = Split-Path -Parent $PSScriptRoot
$agent     = Join-Path $repo 'agent\agent.py'
$launchCmd = Join-Path $repo 'agent\.launch.cmd'
$launchVbs = Join-Path $repo 'agent\.launch.vbs'
$logFile   = Join-Path $repo 'agent\agent.log'

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item $launchCmd, $launchVbs -ErrorAction SilentlyContinue
    Write-Host "已删除计划任务 $taskName"
    return
}

if (-not (Test-Path $agent)) { throw "找不到 Agent 脚本: $agent" }
$python = (Get-Command python.exe -ErrorAction Stop).Source

# 1) cmd 启动器：输出追加到 agent.log，方便排查
$cmdBody = "@echo off`r`nchcp 65001 >nul`r`n`"$python`" `"$agent`" >> `"$logFile`" 2>&1`r`n"
Set-Content -Path $launchCmd -Value $cmdBody -Encoding ASCII

# 2) VBS 启动器：窗口完全隐藏
$vbsBody = @"
Set sh = CreateObject("WScript.Shell")
sh.Run """$launchCmd""", 0, False
"@
Set-Content -Path $launchVbs -Value $vbsBody -Encoding ASCII

# 3) 计划任务：登录时触发，崩溃自动重启
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$launchVbs`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description 'pc2mb Windows Agent（远程桌面被控端）' -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
Write-Host "已注册计划任务 $taskName（登录时自动后台启动）"
Write-Host "Agent 日志: $logFile"
