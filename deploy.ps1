#requires -Version 5
<#
OpenAgentOS 部署（Windows PowerShell / Docker Desktop）：
  同步 GitDir -> DeployDir（排除 .env）→ 构建 → docker compose up → 健康检查。
用法：
  ./deploy.ps1                         # 构建 + 部署（时间戳 tag）
  ./deploy.ps1 -Tag 20260707-1200      # 复用已有镜像，跳过构建
原生 Windows 请传 Windows 路径（-GitDir C:\... -DeployDir C:\...）；WSL2 下用默认 /data 路径。
#>
[CmdletBinding()]
param(
  [string]$GitDir    = $(if ($env:GIT_DIR)    { $env:GIT_DIR }    else { "/data/git/openagentos" }),
  [string]$DeployDir = $(if ($env:DEPLOY_DIR) { $env:DEPLOY_DIR } else { "/data/openagentos" }),
  [string]$HostIp    = $(if ($env:HOST_IP)    { $env:HOST_IP }    else { "host.docker.internal" }),
  [string]$Tag
)
$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "[STEP] $m" -ForegroundColor Blue }
function Ok ($m) { Write-Host "[ OK ] $m" -ForegroundColor Green }
function Die($m) { Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }
function EnvVal($file, $key) {
  $m = Select-String -Path $file -Pattern "^$key=(.*)$" | Select-Object -First 1
  if ($m) { return $m.Matches.Groups[1].Value.Trim() } else { return "" }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Die "docker not found in PATH" }
$envFile = Join-Path $DeployDir ".env"
if (-not (Test-Path (Join-Path $GitDir "Dockerfile"))) { Die "no Dockerfile under $GitDir (git clone first?)" }
New-Item -ItemType Directory -Force -Path $DeployDir | Out-Null
if (-not (Test-Path $envFile)) { Die "missing $envFile -> copy .env.example there and edit" }
if (-not (EnvVal $envFile "POSTGRES_PASSWORD")) { Die "$envFile: POSTGRES_PASSWORD empty" }

$wsHost = EnvVal $envFile "AGENTOS_WORKSPACE_HOST"
if (-not $wsHost) { $wsHost = "/data/openagentos/workspace" }
New-Item -ItemType Directory -Force -Path $wsHost | Out-Null
Ok "workspace dir ready: $wsHost"

Say "sync $GitDir -> $DeployDir (robocopy)"
# /MIR 镜像同步；/XD 排除目录，/XF 排除文件（.env 保留）；退出码 <8 为成功。
$xd = ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"
robocopy $GitDir $DeployDir /MIR /XD $xd /XF ".env" /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) { Die "robocopy failed (code $LASTEXITCODE)" }
Ok "code synced (.env preserved)"

# 注入 host_ip / workspace 到 sandbox.toml。
$toml = Join-Path $DeployDir "sandbox.toml"
(Get-Content $toml) `
  -replace '^host_ip = .*', "host_ip = `"$HostIp`"" `
  -replace '^allowed_host_paths = .*', "allowed_host_paths = ['$wsHost']" |
  Set-Content $toml -Encoding UTF8
Ok "sandbox.toml host_ip=$HostIp workspace=$wsHost"

if ($Tag) { $build = @(); Say "redeploy tag $Tag (skip build)" }
else { $Tag = Get-Date -Format "yyyyMMdd-HHmm"; $build = @("--build"); Say "build + deploy tag $Tag" }
$env:TAG = $Tag
Push-Location $DeployDir
try {
  docker compose up -d @build --remove-orphans
  docker compose up -d --force-recreate opensandbox-server
} finally { Pop-Location }

$app = EnvVal $envFile "PROJECT_NAME"; if (-not $app) { $app = "openagentos" }
Say "waiting for $app healthy ..."
$status = "missing"
foreach ($i in 1..60) {
  $status = (docker inspect $app --format '{{.State.Health.Status}}' 2>$null)
  if (-not $status) { $status = "missing" }
  switch ($status) {
    "healthy"   { Ok "$app healthy"; break }
    "unhealthy" { docker logs --tail 40 $app; Die "$app unhealthy" }
    "missing"   { Die "$app not found - up failed?" }
    default     { Start-Sleep 2 }
  }
}
if ($status -ne "healthy") { Die "$app not healthy within ~120s (docker logs $app)" }
Ok "DEPLOY OK -- tag $Tag"
Say "tail logs: docker logs -f --tail 50 $app"
