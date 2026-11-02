"""全局配置（基础设施层可替换的参数集中在此）。"""
import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

# 打包成 exe（PyInstaller）后 __file__ 位于临时解压目录，退出即删；
# 因此可写数据（数据库/上传/日志）必须落在 exe 同目录，只读资源从包内目录读取。
FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = (Path(sys.executable).resolve().parent if FROZEN
            else Path(__file__).resolve().parent.parent)
# 打包资源（模板等）所在根目录：frozen 时是 PyInstaller 的解压目录
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))


@dataclass
class Settings:
    # 存储路径
    data_dir: Path = BASE_DIR / "data"
    db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "fapiao.db")
    upload_dir: Path = field(default_factory=lambda: BASE_DIR / "data" / "uploads")
    log_dir: Path = field(default_factory=lambda: BASE_DIR / "data" / "logs")

    # 匹配阈值（设计 §3.2 匹配状态机）
    auto_match_threshold: float = 0.95   # score >= 0.95 自动 MATCHED
    suspicious_threshold: float = 0.70   # 0.7 <= score < 0.95 待人工
    candidate_min_score: float = 0.50    # 低于此分不落匹配记录（无候选 -> UNMATCHED）

    # 金额容差（不变量 I5：允许 ±0.01 误差）
    amount_tolerance: Decimal = Decimal("0.01")

    # 汇总匹配（多对一）组合搜索上限
    many_to_one_max_invoices: int = 5
    many_to_one_pool_size: int = 20

    # 默认操作人（本系统为单机工具，无登录体系）
    default_operator: str = "admin"

    # SQLite 同步级别（FULL/NORMAL/OFF）：默认 FULL 保证断电安全。
    # FAPIAO_SYNC=OFF 供特定受限环境（工具沙箱内 E: 盘 fsync 一次 400ms+）启动时降级，
    # 正常终端运行无需设置。
    db_synchronous: str = field(default_factory=lambda: os.getenv("FAPIAO_SYNC", "FULL"))


settings = Settings()
