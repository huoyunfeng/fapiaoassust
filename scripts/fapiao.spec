# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（文件夹模式 / onedir，禁用单文件模式）。

用法（在仓库根目录）：
    python -m PyInstaller --noconfirm --clean --distpath dist --workpath build scripts\\fapiao.spec

产出：dist\\发票报销核对助手\\发票报销核对助手.exe
      —— 数据不落在 exe 目录，而写入 %APPDATA%\\FapiaoAssistant（见 app/config.py）

为什么必须 onedir：
  · 单文件模式（-F/--onefile）运行时要把自身解压到 %TEMP% 再执行，
    行为与常见恶意"自解压释放器"高度相似，是杀毒软件的重点误报对象；
  · onedir 目录结构透明、启动快、无临时解压，误报率显著更低。
"""
import os

from PyInstaller.utils.hooks import collect_all

# SPECPATH 是 spec 文件所在目录（scripts\），其上一级即仓库根目录
ROOT = os.path.dirname(os.path.abspath(SPECPATH))

APP_NAME = "发票报销核对助手"

datas = [(os.path.join(ROOT, "app", "web", "templates"), "app/web/templates")]
binaries = []
# filelock 为桌面启动器的函数内导入，显式声明防止漏打包
hiddenimports = ["filelock"]

# pywebview 的 EdgeChromium 后端通过 pythonnet 调用 .NET，运行时才 `import clr`：
# 缺了 clr / pythonnet 模块，打包后创建窗口会抛异常，而 console=False 会吞掉堆栈，
# 表现为「启动几秒后静默退出」。这里显式声明并连带收集 pythonnet 运行时资源。
hiddenimports += ["clr", "pythonnet", "clr_loader", "pythonnet.runtime"]

# 需要连同子模块/数据一起打进去的第三方包：
# - rapidocr_onnxruntime：内含识别模型与 config.yaml
# - onnxruntime：推理引擎 DLL
# - uvicorn：loops/protocols 等运行时按字符串导入
# - pdfminer / pypdfium2：PDF 解析与 CMap 数据
# - webview：桌面窗口（pywebview），按平台动态导入
for pkg in ("rapidocr_onnxruntime", "onnxruntime", "uvicorn",
            "pdfminer", "pypdfium2", "webview", "pythonnet", "clr_loader"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# 可选图标：存在 .ico 时自动启用（assets\\app_icon.ico）
_icon = os.path.join(ROOT, "assets", "app_icon.ico")
icon = _icon if os.path.exists(_icon) else None

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,      # 关键：二进制交给 COLLECT，产出文件夹而非单文件
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # UPX 压缩会被杀毒引擎判定为加壳，必须关闭
    console=False,              # 隐藏控制台黑框（等价 -w / --noconsole）
    icon=icon,
    version=os.path.join(ROOT, "scripts", "version_info.txt"),  # exe 版本资源
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
