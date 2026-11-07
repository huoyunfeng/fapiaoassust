"""Windows 长路径支持。

Windows 默认 MAX_PATH=260，超出后 os/pathlib 调用会抛 WinError 206。
在不修改注册表 `LongPathsEnabled` 的前提下，对绝对路径加 `\\\\?\\` 前缀即可绕过
（UNC 路径加 `\\\\?\\UNC\\`）。这里只在真正落盘/读取的最后一刻转换，
避免污染数据库里保存的普通路径。
"""
import os
from pathlib import Path
from typing import Iterator, Union

_MAX = 240  # 预留文件名与扩展名余量

PathLike = Union[str, os.PathLike]


def fs_path(path: PathLike) -> str:
    """返回可直接传给 open/os/FileResponse 的路径字符串（按需要加长路径前缀）。"""
    s = os.fspath(path)
    if os.name != "nt":
        return s
    s = os.path.abspath(s)
    if s.startswith("\\\\?\\"):
        return s
    if len(s) < _MAX:
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s.lstrip("\\")
    return "\\\\?\\" + s


def plain_path(path: PathLike) -> Path:
    """去掉长路径前缀，便于 pathlib 做相对路径计算与展示。"""
    s = os.fspath(path)
    if os.name == "nt":
        if s.startswith("\\\\?\\UNC\\"):
            s = "\\\\" + s[len("\\\\?\\UNC\\"):]
        elif s.startswith("\\\\?\\"):
            s = s[len("\\\\?\\"):]
    return Path(s)


def iter_files(root: PathLike) -> Iterator[Path]:
    """递归遍历目录下的文件，兼容超过 260 字符的深层目录。

    产出的是去前缀的普通 Path，可直接参与 relative_to 计算。
    """
    for dirpath, _dirnames, filenames in os.walk(fs_path(root)):
        for name in filenames:
            yield plain_path(os.path.join(dirpath, name))


def read_bytes(path: PathLike) -> bytes:
    with open(fs_path(path), "rb") as f:
        return f.read()


def write_bytes(path: PathLike, content: bytes) -> None:
    with open(fs_path(path), "wb") as f:
        f.write(content)
