"""全局配置（基础设施层可替换的参数集中在此）。"""
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


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
