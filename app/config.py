"""全局配置（基础设施层可替换的参数集中在此）。"""
import json
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

# 软件版本（备份 manifest 强校验用；发包时递增）
APP_VERSION = "1.2.0"
# 数据库结构版本（恢复时低版本备份自动触发迁移）
SCHEMA_VERSION = 3
# 离线授权码签名密钥（对称 HMAC；商业发布前请替换为随机长串并妥善保管）
LICENSE_SECRET = "fapiao-assust-offline-license-key-v1"

# 用户级持久化配置（附件路径等自定义项），备份时随包带走
CONFIG_FILE = BASE_DIR / "config.json"


def load_persisted_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_persisted_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                           encoding="utf-8")


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

    # 默认操作人（未登录场景/系统自动任务使用）
    default_operator: str = "admin"

    # ---------------------------------------------------------------- 商业化与授权
    # 云端发码服务（私钥仅存在于云端，本机只内置公钥）
    # 注意：域名 fapiao-assist.xyz 未完成 ICP 备案，会被阿里云拦截返回 403，
    # 因此默认直连已备案可用的服务器 IP；后续域名备案通过可用 FAPIAO_CLOUD_URL 覆盖。
    cloud_base_url: str = field(
        default_factory=lambda: os.getenv("FAPIAO_CLOUD_URL", "http://182.92.198.99"))
    cloud_timeout: float = 8.0          # 激活请求 8 秒超时（设计要求）
    # 本机内置公钥（可公开）；私钥绝不下发到客户端，只存在于云端 license_server.py
    license_public_key_pem: bytes = b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAl4ssLkySALA4wsexQNFXF2ROMhU+yhrVvXbgaXx1jyY=
-----END PUBLIC KEY-----"""
    initial_password: str = "123456"  # 超管解锁码重置后的初始密码（登录后强制改密）
    free_monthly_invoice_quota: int = 20  # 免费版每月发票额度
    # 收款二维码图片地址（留空则前端展示"联系客服获取收款码"占位说明）
    payment_qr_url: str = field(default_factory=lambda: os.getenv("FAPIAO_PAY_QR_URL", ""))

    # SQLite 同步级别（FULL/NORMAL/OFF）：默认 FULL 保证断电安全。
    # FAPIAO_SYNC=OFF 供特定受限环境（工具沙箱内 E: 盘 fsync 一次 400ms+）启动时降级，
    # 正常终端运行无需设置。
    db_synchronous: str = field(default_factory=lambda: os.getenv("FAPIAO_SYNC", "FULL"))

    # 开发者调试模式：绕过授权码与免费版额度校验（仅本地调试用）
    dev_mode: bool = False


settings = Settings()

# 启动时应用用户自定义附件路径（P2-4：附件存储路径自定义）
_persisted = load_persisted_config()
if _persisted.get("upload_dir"):
    try:
        settings.upload_dir = Path(_persisted["upload_dir"])
    except Exception:
        pass

# 开发者调试模式（仅供开发自测，绕过授权码与免费版额度）：
#   ① 环境变量 FAPIAO_DEV_MODE=1 —— 源码运行与打包 exe 均生效；
#   ② config.json 的 "dev_mode": true —— 仅源码运行生效（FROZEN 时忽略）。
# 打包后的 exe 只认环境变量，避免开发开关被误带到用户机器上。
settings.dev_mode = (
    os.getenv("FAPIAO_DEV_MODE", "").strip().lower() in ("1", "true", "yes", "on")
    or (bool(_persisted.get("dev_mode")) and not FROZEN)
)
