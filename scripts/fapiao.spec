# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单文件打包配置。

用法（在仓库根目录）：
    python -m PyInstaller --noconfirm --clean --distpath dist --workpath build scripts\\fapiao.spec

产出：dist\\fapiao-check.exe（首次运行会在 exe 同目录创建 data\\ 存库/上传/日志）
"""
import os

from PyInstaller.utils.hooks import collect_all

# SPECPATH 是 spec 文件所在目录（scripts\），其上一级即仓库根目录
ROOT = os.path.dirname(os.path.abspath(SPECPATH))

datas = [(os.path.join(ROOT, "app", "web", "templates"), "app/web/templates")]
binaries = []
hiddenimports = []

# 需要连同子模块/数据一起打进去的第三方包：
# - rapidocr_onnxruntime：内含识别模型与 config.yaml
# - onnxruntime：推理引擎 DLL
# - uvicorn：loops/protocols 等运行时按字符串导入
# - pdfminer / pypdfium2：PDF 解析与 CMap 数据
for pkg in ("rapidocr_onnxruntime", "onnxruntime", "uvicorn",
            "pdfminer", "pypdfium2"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    [os.path.join(ROOT, "run.py")],
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
    a.binaries,
    a.datas,
    [],
    name="fapiao-check",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
