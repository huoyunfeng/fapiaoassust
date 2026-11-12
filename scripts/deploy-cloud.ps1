# 部署云端授权服务：上传 license_server.py → 语法检查 → 热重启 → 健康校验（失败自动回滚）
# 用法（在项目根目录执行，首次会提示输入服务器密码）：
#   powershell -ExecutionPolicy Bypass -File scripts\deploy-cloud.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\deploy-cloud.ps1 -Server root@1.2.3.4
param(
    [string]$Server = "root@182.92.198.99",
    [string]$RemoteDir = "/opt/fapiao-license",
    [string]$Service = "fapiao-license"
)

$ErrorActionPreference = "Stop"
$localFile = Join-Path $PSScriptRoot "..\cloud\license_server.py"
$publicHealth = "http://182.92.198.99/health"

if (-not (Test-Path $localFile)) { throw "找不到本地文件：$localFile" }

Write-Host "==> 1/3 上传到 $Server`:$RemoteDir/license_server.py.new（会提示输入密码）" -ForegroundColor Cyan
scp $localFile "${Server}:${RemoteDir}/license_server.py.new"
if ($LASTEXITCODE -ne 0) { throw "上传失败（退出码 $LASTEXITCODE）" }

# 远端：先语法检查再替换；重启后 3 秒内健康检查不通过则自动恢复旧文件
Write-Host "==> 2/3 语法检查 + 重启服务（失败自动回滚）" -ForegroundColor Cyan
$remoteCmd = "cd $RemoteDir && python3 -m py_compile license_server.py.new && " +
    "cp -f license_server.py license_server.py.bak && " +
    "mv -f license_server.py.new license_server.py && " +
    "systemctl restart $Service && sleep 3 && " +
    "systemctl is-active $Service && curl -fsS -m 5 http://127.0.0.1/health || " +
    "(echo ROLLED_BACK; cp -f license_server.py.bak license_server.py; " +
    "systemctl restart $Service; exit 1)"
ssh $Server $remoteCmd
if ($LASTEXITCODE -ne 0) { throw "远端部署失败，已回滚到 license_server.py.bak" }

Write-Host "==> 3/3 公网健康检查 $publicHealth" -ForegroundColor Cyan
$resp = Invoke-WebRequest -Uri $publicHealth -UseBasicParsing -TimeoutSec 8
Write-Host ("部署成功：" + $resp.Content) -ForegroundColor Green
