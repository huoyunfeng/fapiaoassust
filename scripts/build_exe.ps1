# 打包为单文件 exe（PyInstaller）：dist\<Name>.exe
# 用法：powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
#       powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -Name fapiao-check
param([string]$Name = "fapiao-check")

Set-Location (Split-Path $PSScriptRoot -Parent)
if ($LASTEXITCODE) { exit $LASTEXITCODE }

# --specpath build 会让 --add-data 的相对源路径按 build\ 解析，故这里用绝对路径
$templates = Join-Path (Get-Location) "app\web\templates"

python -m PyInstaller --noconfirm --clean --onefile --console --name $Name `
  --distpath dist --workpath build --specpath build `
  --add-data "$templates;app/web/templates" `
  --collect-all rapidocr_onnxruntime `
  --collect-all onnxruntime `
  --collect-all uvicorn `
  --collect-all pdfminer `
  --collect-all pypdfium2 `
  --exclude-module tkinter `
  --exclude-module pytest `
  run.py

if ($LASTEXITCODE -ne 0) { Write-Host "打包失败"; exit $LASTEXITCODE }
Write-Host "已生成 dist\$Name.exe"
