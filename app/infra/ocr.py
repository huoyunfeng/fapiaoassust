"""OCR 基础设施：为图片发票（jpg/png）注册解析后端。

实现约定：
- 领域层 OcrParser 只声明接口，具体引擎在此注册（基础设施可替换）；
- 当前后端为 RapidOCR（onnxruntime，离线可用、随包分发模型）；
- 识别出的文本复用 domain.parsers.extract_fields_from_text，与 PDF 口径一致。

性能（实测中位数，i5-8250U / 4核，1400x900 与 2800x1800 高清图）：
- 关闭方向分类 use_cls=false（发票方向固定），省 ~20ms；
- 全局预缩放 max_side_len=960 + det 限幅 limit_type=max/960：
  det 输入像素降为 1/2~1/5，det 644ms -> 354ms（小图）、1461ms -> 359ms（高清图），
  高清图总耗时 -55%，识别结果与不缩放逐字一致；
- rec_batch_num=1（默认 6）：rec 1429ms -> 950ms（-34%）。
  原因见 rapidocr 的 TextRecognizer：同一批 crop 会被补零到批内最宽那条的宽度，
  发票里长短行混排，批内其它行有相当比例的算力花在补零上；单条送检则按自身宽度算。
  实测低分辨率图反而更准（税号 J/U 一个易混字符），高清图逐字一致。
- 启动时用一张 960x617、20 行的合成发票图预热 det+rec，消除首单冷启动延迟。
"""
import io
import re
import tempfile
from pathlib import Path

from app.domain.dto import InvoiceDTO
from app.domain.parsers import ParseSkip, extract_fields_from_text, ocr_parser_singleton
from app.infra.logger import get_logger

logger = get_logger(__name__)

_engine = None          # RapidOCR 实例（惰性加载，register_ocr_backend 时预热）
_backend_registered = False
_CONFIG_PATH: Path | None = None   # 我们生成的优化版配置文件

# 图像长边上限：超过则等比缩放后再识别。
# 960 是实测平衡点——det 像素量大幅下降，而 A4 发票正文在缩放后仍有 ~18px，
# 识别结果与不缩放逐字一致；再小会开始丢字。
MAX_SIDE_LEN = 960


def _locate_default_yaml() -> Path:
    import rapidocr_onnxruntime as _r
    pkg = Path(_r.__file__).parent
    candidate = pkg / "config.yaml"
    return candidate if candidate.exists() else Path.cwd() / "config.yaml"


def _patch_yaml(raw: str, patches: dict) -> str:
    """按 {'Global.max_side_len': 960} 逐行打补丁。

    只改目标 key 的值，保留其余全部字段——手写整份 yaml 会遗漏
    limit_type / use_dilation / score_mode 等 det 参数，实测会变慢 2 倍。
    """
    todo = {}
    for dotted, val in patches.items():
        section, key = dotted.split(".")
        todo.setdefault(section, {})[key] = str(val)

    out, cur = [], None
    for line in raw.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and not line[0].isspace() and stripped.endswith(":"):
            cur = stripped[:-1]
            out.append(line)
            continue
        if cur in todo:
            m = re.match(r"^([ \t]+)(\S+):[ \t]*(.*?)([ \t]*)(\r?\n)?$", line)
            if m and m.group(2) in todo[cur]:
                out.append(f"{m.group(1)}{m.group(2)}: {todo[cur].pop(m.group(2))}"
                           f"{m.group(5) or ''}")
                continue
        out.append(line)

    left = {s: k for s, k in todo.items() if k}
    if left:
        raise RuntimeError(f"OCR yaml 未找到待改字段: {left}")
    return "".join(out)


def _build_config() -> Path:
    """从已安装包的 config.yaml 复制并微调，生成优化配置（进程内只生成一次）。"""
    global _CONFIG_PATH
    if _CONFIG_PATH is not None and _CONFIG_PATH.exists():
        return _CONFIG_PATH

    raw = _locate_default_yaml().read_text(encoding="utf-8")
    patches = {
        "Global.use_cls": "false",              # 发票方向固定，跳过方向分类
        "Global.max_side_len": MAX_SIDE_LEN,    # 大图先等比缩小，det 像素量骤降
        "Det.limit_type": "max",                # 默认 min 只在短边过小时放大，几乎不缩图
        "Det.limit_side_len": MAX_SIDE_LEN,
        # 逐条送 rec：同批 crop 要补零到批内最宽那条，长短行混排时大半算力是零
        "Rec.rec_batch_num": 1,
    }
    raw = _patch_yaml(raw, patches)

    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    f.write(raw); f.close()
    _CONFIG_PATH = Path(f.name)
    logger.info("OCR 配置已生成 (use_cls=false, max_side_len=%s, rec_batch=1): %s",
                MAX_SIDE_LEN, _CONFIG_PATH)
    return _CONFIG_PATH


def _warmup(engine) -> None:
    """用一张「典型发票」图预热 det+rec 两个 onnxruntime session。

    三个要点（都是实测踩过的坑）：
    - 必须走完整 det+rec：use_det=False 只暖 rec，det 仍是冷的；
    - 尺寸要等于真实输入：1400x900 的发票缩放后约 960x617，det 输入 960x608，
      预热图用这个尺寸才能命中同一个 shape；
    - 行数要接近真实：onnxruntime 按 rec 每个 crop 的 shape 分别做图优化，
      只画 1 行文字时，首张 20 行的真实发票仍要为其余 crop 重建（多付 1-2s）。
    """
    try:
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
        W, H = MAX_SIDE_LEN, 617
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 22)
        except Exception:
            font = ImageFont.load_default()
        # 20 行、宽度参差，尽量覆盖真实发票的 crop 形状
        rows = [
            "增值税普通发票", "发票号码：24412000000002045090", "开票日期：2024年10月15日",
            "购买方信息", "名称：北京示例科技有限公司", "税号：91110108MA01ABCDEF",
            "销售方信息", "名称：上海服务有限公司", "税号：91310115MA1GHIJK89",
            "项目名称", "金额", "税率", "税额",
            "信息技术咨询服务  15841.58  6%  950.50", "合计：¥15841.58", "¥950.50",
            "价税合计（大写）壹万陆仟柒佰玖拾贰元零捌分", "(小写)¥16792.08",
            "开票人：admin", "备注：测试",
        ]
        for i, text in enumerate(rows):
            d.text((20, 10 + i * 30), text, fill="black", font=font)
        result = engine(np.array(img))
        if not result:
            logger.warning("OCR 预热未识别到文字，引擎可能异常")
        logger.info("OCR 引擎预热完成（det+rec 全链路, %sx%s, %d 行）",
                    W, H, len(rows))
    except Exception as e:
        logger.warning("OCR 预热跳过: %s", e)


def _load_engine(warmup: bool = True):
    """惰性加载 RapidOCR；未安装时抛 ParseSkip 走降级链。"""
    global _engine
    if _engine is not None:
        return _engine
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as e:
        raise ParseSkip(
            f"OCR 引擎 RapidOCR 未安装（pip install rapidocr-onnxruntime）: {e}"
        ) from e
    cfg = _build_config()
    _engine = RapidOCR(config_path=str(cfg))
    if warmup:
        _warmup(_engine)
    return _engine


def _image_to_array(content: bytes):
    """图片字节 -> RGB numpy 数组（按 EXIF 方向校正）。"""
    try:
        import numpy as np
        from PIL import Image, ImageOps
    except ImportError as e:
        raise ParseSkip(f"OCR 依赖缺失（Pillow/numpy）: {e}") from e
    try:
        img = Image.open(io.BytesIO(content))
        img = ImageOps.exif_transpose(img)   # 手机照片方向校正
        img = img.convert("RGB")
    except Exception as e:
        raise ParseSkip(f"OCR 图片无法解码: {e}") from e
    return np.array(img)


def parse_image(content: bytes) -> InvoiceDTO:
    """OCR 解析图片发票，返回未标准化 DTO。"""
    import time as _t
    t0 = _t.time()
    engine = _load_engine(warmup=False)
    arr = _image_to_array(content)
    result, elapse = engine(arr)
    if not result:
        raise ParseSkip("OCR 未识别到文字（图片可能过于模糊）")

    # result: [[框坐标, 文本, 置信度], ...]，按行拼接供字段抽取
    lines = []
    for item in result:
        text = (item[1] or "").strip() if len(item) > 1 else ""
        if text:
            lines.append(text)
    text_all = "\n".join(lines)

    dto = extract_fields_from_text(text_all)
    if not dto.invoice_no and not dto.total_amount:
        raise ParseSkip(
            "OCR 已识别文字，但未提取到关键字段（发票号码/价税合计）"
        )
    total_ms = (_t.time() - t0) * 1000
    det_ms = cls_ms = rec_ms = 0
    if isinstance(elapse, (list, tuple)) and len(elapse) >= 3:
        det_ms, cls_ms, rec_ms = elapse[0] * 1000, elapse[1] * 1000, elapse[2] * 1000
    logger.info(
        "OCR 识别完成: 行数=%s  发票号=%s  总耗时=%.0fms (det=%.0f cls=%.0f rec=%.0f)",
        len(lines), dto.invoice_no, total_ms, det_ms, cls_ms, rec_ms,
    )
    return dto


def register_ocr_backend() -> None:
    """注册 OCR 后端（幂等，重复调用无副作用）。"""
    global _backend_registered
    if _backend_registered:
        return
    ocr_parser_singleton.register_backend(parse_image)
    _backend_registered = True
    # 立即加载 + 预热，把 ~600ms 冷启动放到服务启动阶段，不阻塞首单
    try:
        _load_engine(warmup=True)
    except ParseSkip:
        logger.warning("OCR 引擎未就绪，将在首次图片发票导入时再尝试加载")
    logger.info("OCR 后端已注册: RapidOCR（jpg/png 发票导入可用）")
