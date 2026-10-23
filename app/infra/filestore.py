"""文件存储基础设施：保存上传文件、计算哈希、识别类型。可替换。"""
import hashlib
import re
import uuid
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.infra.logger import get_logger

logger = get_logger(__name__)

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

    def delete(self, path: str) -> bool:
        """删除已落盘的物理文件（删除记录后的收尾清理）。

        容错要求（业务规则 3）：文件被占用、不存在或任何其它异常都只记日志，
        绝不抛错 —— 数据库记录已删即视为删除成功，文件残留可后续人工清理。
        安全约束：仅允许删除上传目录内的文件，防止脏数据导致误删任意路径。
        """
        if not path:
            return False
        try:
            p = Path(path).resolve()
            root = self.root.resolve()
            if p != root and root not in p.parents:
                logger.warning("拒绝删除上传目录外的文件: %s", path)
                return False
            p.unlink(missing_ok=True)
            logger.info("物理文件已删除: %s", p)
            return True
        except Exception as e:
            logger.warning("物理文件删除失败（已忽略，不影响数据删除）: %s - %s", path, e)
            return False
