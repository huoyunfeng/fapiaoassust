# 打包为「文件夹模式」桌面程序（PyInstaller onedir）：dist\<Name>\<Name>.exe
# 用法：powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
#       powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -Name fapiao-check
#
# 注意：绝对不要改回 --onefile（单文件）。单文件运行时需自解压到 %TEMP%，
#       是杀毒软件误报的重点对象；onedir 目录结构透明、启动快、误报率低。
#
# 可选代码签名（降低杀毒误报 / SmartScreen 拦截）：
#   1) 准备代码签名证书（.pfx），推荐 OV/EV 证书
#   2) 设置环境变量后打包：
#        $env:FAPIAO_SIGN_CERT="D:\cert\fapiao.pfx"
#        $env:FAPIAO_SIGN_PASSWORD="证书密码"
#        powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
#   3) 未配置证书时只提示跳过，不影响打包
param(
    [string]$Name = "发票报销核对助手",
    [string]$CertPath = $env:FAPIAO_SIGN_CERT,
    [string]$CertPassword = $env:FAPIAO_SIGN_PASSWORD,
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

Set-Location (Split-Path $PSScriptRoot -Parent)
if ($LASTEXITCODE) { exit $LASTEXITCODE }

# 统一走 spec 配置：onedir / 禁用 UPX / 隐藏控制台 / 图标 / 版本资源均在 spec 内定义
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build scripts\fapiao.spec

if ($LASTEXITCODE -ne 0) { Write-Host "打包失败"; exit $LASTEXITCODE }

$exe = Join-Path (Get-Location) "dist\$Name\$Name.exe"
Write-Host "已生成 $exe"

# ---------------- 代码签名（可选） ----------------
function Find-SignTool {
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $roots = @(
        "${env:ProgramFiles(x86)}\Windows Kits\10\bin",
        "$env:ProgramFiles\Windows Kits\10\bin"
    )
    foreach ($root in $roots) {
        if (Test-Path $root) {
            $hit = Get-ChildItem $root -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
                   Where-Object { $_.FullName -match "x64" } |
                   Sort-Object FullName -Descending | Select-Object -First 1
            if ($hit) { return $hit.FullName }
        }
    }
    return $null
}

if (-not $CertPath) {
    Write-Host ""
    Write-Host "[签名] 未配置证书，跳过代码签名。" -ForegroundColor Yellow
    Write-Host "        未签名的 exe 可能被杀毒软件误报、被 SmartScreen 拦截。" -ForegroundColor Yellow
    Write-Host "        配置 FAPIAO_SIGN_CERT / FAPIAO_SIGN_PASSWORD 后重新打包可自动签名。"
    exit 0
}
if (-not (Test-Path $CertPath)) {
    Write-Host "[签名] 证书不存在：$CertPath" -ForegroundColor Red
    exit 1
}
if (-not $CertPassword) {
    Write-Host "[签名] 已提供证书但缺少密码（FAPIAO_SIGN_PASSWORD）。" -ForegroundColor Red
    exit 1
}

$signTool = Find-SignTool
if (-not $signTool) {
    Write-Host "[签名] 未找到 signtool.exe。" -ForegroundColor Red
    Write-Host "        请安装 Windows SDK（含签名工具）后重试，或手动执行：" -ForegroundColor Red
    Write-Host "        signtool sign /f `"$CertPath`" /p *** /fd SHA256 /tr $TimestampUrl /td SHA256 `"$exe`"" -ForegroundColor Red
    exit 1
}

Write-Host "[签名] 使用 $signTool"
& $signTool sign /f "$CertPath" /p "$CertPassword" /fd SHA256 /tr "$TimestampUrl" /td SHA256 "$exe"
if ($LASTEXITCODE -ne 0) { Write-Host "[签名] 签名失败" -ForegroundColor Red; exit $LASTEXITCODE }

$sig = Get-AuthenticodeSignature $exe
if ($sig.Status -ne "Valid") {
    Write-Host "[签名] 校验未通过：$($sig.Status) $($sig.StatusMessage)" -ForegroundColor Red
    exit 1
}
Write-Host "[签名] 完成并校验通过：$($sig.SignerCertificate.Subject)" -ForegroundColor Green
