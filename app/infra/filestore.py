"""文件存储基础设施：保存上传文件、计算哈希、识别类型。可替换。"""
import hashlib
import re
import uuid
from datetime import datetime
from pathlib import Path

from app.config import settings

SUPPORTED_TYPES = {"pdf", "ofd", "xml", "jpg", "jpeg", "png"}


class FileStore:
    """上传文件落盘 + 指纹。目录结构：uploads/YYYYMM/<uuid>_<安全文件名>。"""

    def __init__(self, root: Path = None) -> None:
        self.root = root or settings.upload_dir

    def detect_type(self, filename: str) -> str:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return ext

    def is_supported(self, filename: str) -> bool:
        return self.detect_type(filename) in SUPPORTED_TYPES

    def sha256(self, content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def save(self, filename: str, content: bytes) -> Path:
        month_dir = self.root / datetime.now().strftime("%Y%m")
        month_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", filename)[:80] or "file"
        path = month_dir / f"{uuid.uuid4().hex[:8]}_{safe}"
        path.write_bytes(content)
        return path
