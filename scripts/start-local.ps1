param(
  [int]$CorePort = 18765,
  [int]$WebPort = 5173,
  [string]$SessionToken = "manual-test-token",
  [string]$DataDir = ".local-data",
  # 附加启动桌面壳（Electron dev 模式，直连本脚本拉起的 Web/Core）
  [switch]$Desktop
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $root ".runtime-local"
$dataPath = if ([System.IO.Path]::IsPathRooted($DataDir)) { $DataDir } else { Join-Path $root $DataDir }
New-Item -ItemType Directory -Force -Path $runtimeDir, $dataPath | Out-Null

Write-Host "Starting Core API on http://127.0.0.1:$CorePort"
$core = Start-Process -PassThru -WindowStyle Hidden -WorkingDirectory $root `
  -FilePath "uv" -ArgumentList @(
    "--cache-dir", (Join-Path $root ".uv-cache"), "run", "--directory", "apps/core-api",
    "python", "-m", "investment_steward_core", "--host", "127.0.0.1", "--port", $CorePort,
    "--data-dir", $dataPath, "--session-token", $SessionToken
  )

Write-Host "Starting Web Shell on http://127.0.0.1:$WebPort"
$web = Start-Process -PassThru -WindowStyle Hidden -WorkingDirectory $root `
  -FilePath "pnpm.cmd" -ArgumentList @("--filter", "@investment-steward/web-shell", "dev", "--", "--host", "127.0.0.1", "--port", $WebPort)

# Agent Worker：每日简报 / 证据新鲜度巡逻 / 通知评估落库与外部投递的调度方。
# 此前 dev 本地从不启动它，三个自动化任务只能手动触发（2026-09-07 用户拍板补上）。
Write-Host "Starting Agent Worker (tick=60s, core=http://127.0.0.1:$CorePort)"
$worker = Start-Process -PassThru -WindowStyle Hidden -WorkingDirectory $root `
  -FilePath "uv" -ArgumentList @(
    "--cache-dir", (Join-Path $root ".uv-cache"), "run", "--directory", "apps/agent-worker",
    "python", "-m", "investment_steward_agent_worker",
    "--status-file", (Join-Path $dataPath "agent-worker.status.json"),
    "--core-url", "http://127.0.0.1:$CorePort",
    "--session-token", $SessionToken
  )

if ($Desktop) {
  # dev 模式桌面壳：STEWARD_WEB_URL 直连本脚本的 Web 端口（与 desktop-host main.ts 的 devMode 判定一致）。
  $env:ELECTRON_RUN_AS_NODE = $null
  $env:STEWARD_WEB_URL = "http://127.0.0.1:$WebPort"
  $electron = Join-Path $root "apps\desktop-host\node_modules\electron\dist\electron.exe"
  if (Test-Path $electron) {
    Start-Process -WorkingDirectory (Join-Path $root "apps\desktop-host") -FilePath $electron `
      -ArgumentList @(".", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage") | Out-Null
    Write-Host "Desktop shell launched (dev mode → $env:STEWARD_WEB_URL)"
  } else {
    Write-Warning "electron.exe 不存在，先执行 pnpm install（apps/desktop-host）"
  }
}

@{
  core_pid = $core.Id
  web_pid = $web.Id
  worker_pid = $worker.Id
  core_url = "http://127.0.0.1:$CorePort"
  web_url = "http://127.0.0.1:$WebPort"
  data_dir = $dataPath
  session_token = $SessionToken
} | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $runtimeDir "local-processes.json")

Write-Host "Local services started (core / web / worker). Process metadata: $runtimeDir\local-processes.json"
