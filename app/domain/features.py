"""无票号场景的业务特征提取与文本碰撞。

设计红线（业务要求）：
- 绝不使用自增 ID / Excel 序号做匹配依据，序号会因增删行重排；
- 只从业务字段建立关联：金额、日期、事由、起止地点、车牌号、票面文字；
- 报销单侧特征来自 extra_data（模板未映射列会自动落此）或备注；
- 发票侧特征来自销方名称、报销内容、OCR 票面文字。

这些函数是纯函数，供匹配引擎与展示层共用。
"""
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Tuple

from app.domain.dto import InvoiceDTO, ReimburseDTO

# 模板/扩展字段的键别名（按优先级取第一个非空值）
REASON_KEYS = ("事由", "报销事由", "出差事由", "用途", "原因", "摘要", "备注")
ORIGIN_KEYS = ("起始地点", "出发地点", "出发地", "起点", "上车地点", "起始地", "始发地")
DEST_KEYS = ("到达地点", "目的地", "终点", "下车地点", "到达地", "收车地点")
PLATE_KEYS = ("车牌号", "车牌", "车辆牌号")

# 「A到B」「A->B」「A→B」等线路写法
_ROUTE_SPLIT = re.compile(r"\s*(?:->|→|—>|-->|到|至|～|~|—|–)\s*")
# 地点/词组分隔符
_LIST_SPLIT = re.compile(r"[\s、,，;；/／|]+")
# 泛词黑名单：这些词在任何交通票上都可能出现，不能作为有效文本命中
_STOP_TERMS = {
    "报销", "费用", "服务", "发票", "公司", "有限", "交通", "打车", "出租",
    "行程", "用车", "乘车", "工作", "外出", "材料", "地点", "出发", "到达",
    "起点", "终点", "备注", "说明", "其他", "tbd", "未知", "待定",
}
_PLACE_EMPTY = {"", "tbd", "未知", "待定", "无", "空", "—", "-"}


@dataclass
class ReimburseFeatures:
    """报销单侧的业务特征集合。"""
    reason: str = ""
    origin: str = ""
    destination: str = ""
    plate_no: str = ""
    ride_time: str = ""
    phrases: List[str] = field(default_factory=list)   # 参与文本碰撞的词条

    @property
    def has_text(self) -> bool:
        return bool(self.phrases)


@dataclass
class InvoiceFeatures:
    """发票侧的票面文本特征。"""
    plate_no: str = ""
    ride_time: str = ""
    text: str = ""

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())


def _norm(text: object) -> str:
    """文本标准化：去所有空白并转小写（中文不受影响）。"""
    return "".join(str(text or "").split()).lower()


def _pick(extra: dict, keys: Iterable[str]) -> str:
    """按别名优先级从 extra_data 中取第一个非空值。"""
    for key in keys:
        val = (extra or {}).get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _clean_place(text: str) -> str:
    """清洗地点：去首尾标点，过滤 TBD/未知等占位词。"""
    t = str(text or "").strip().strip("：:，,。.;；-_/\\")
    return "" if _norm(t) in _PLACE_EMPTY else t


def _split_phrases(text: str) -> List[str]:
    """把一段文本拆成词条：先按列表分隔符，再过滤泛词/占位词。"""
    out: List[str] = []
    for part in _LIST_SPLIT.split(str(text or "")):
        p = _clean_place(part)
        if len(p) < 2:
            continue
        if _norm(p) in _STOP_TERMS:
            continue
        if p not in out:
            out.append(p)
    return out


def _parse_route(text: str) -> Tuple[str, str]:
    """从「TBD到盈创动力E座」「A -> B」这类自由文本中拆出起止地点。

    两侧原文都必须存在（「TBD」算占位但保留位置，清洗后为空）；
    只有一个端点（如「到机场」）无法判断路线，返回空。
    """
    parts = [str(p).strip().strip("：:，,。.;；-_/\\")
             for p in _ROUTE_SPLIT.split(str(text or ""))]
    if len(parts) >= 2 and parts[0] and parts[-1]:
        return _clean_place(parts[0]), _clean_place(parts[-1])
    return "", ""


def with_reimburse_features(reb: ReimburseDTO) -> ReimburseDTO:
    """补齐报销单的业务特征（原地写 DTO 字段，返回同一对象）。

    优先级：显式字段 > extra_data（模板自定义列）> 备注/费用类型。
    起止地点缺失时，尝试从「事由」或备注里的「A到B」写法解析。
    """
    extra = reb.extra_data or {}
    if not reb.reason:
        reb.reason = _pick(extra, REASON_KEYS) or (reb.remark or "")
    if not reb.origin:
        reb.origin = _clean_place(_pick(extra, ORIGIN_KEYS))
    if not reb.destination:
        reb.destination = _clean_place(_pick(extra, DEST_KEYS))
    if not reb.plate_no:
        reb.plate_no = _clean_place(_pick(extra, PLATE_KEYS))
    # 显式字段同样清洗占位词（TBD / 未知），避免「占位符」参与文本碰撞
    reb.origin = _clean_place(reb.origin)
    reb.destination = _clean_place(reb.destination)
    reb.plate_no = _clean_place(reb.plate_no)
    if not (reb.origin and reb.destination):
        # 事由/备注里可能写成「TBD到盈创动力E座」
        o, d = _parse_route(reb.reason or reb.remark or "")
        reb.origin = reb.origin or o
        reb.destination = reb.destination or d
    return reb


def reimburse_features(reb: ReimburseDTO) -> ReimburseFeatures:
    """提取报销单的词条集合（事由、起点、终点、车牌）。"""
    with_reimburse_features(reb)
    phrases: List[str] = []
    for text in (reb.origin, reb.destination, reb.reason, reb.plate_no):
        for p in _split_phrases(text):
            if p not in phrases:
                phrases.append(p)
    return ReimburseFeatures(
        reason=reb.reason or "", origin=reb.origin or "",
        destination=reb.destination or "", plate_no=reb.plate_no or "",
        ride_time="", phrases=phrases,
    )


def invoice_features(inv: InvoiceDTO) -> InvoiceFeatures:
    """提取发票侧票面文本（销方 + 报销内容 + OCR 关键文字 + 车牌）。"""
    text = " ".join(x for x in (inv.seller_name, inv.item_summary,
                                inv.traffic_text, inv.plate_no) if x)
    return InvoiceFeatures(plate_no=inv.plate_no or "",
                           ride_time=inv.ride_time or "",
                           text=text)


def _ngrams(phrase: str, max_n: int = 4) -> Iterable[str]:
    """生成 4/3/2 字的连续子串（长词优先命中，避免「盈创动力」对不上「盈创」）。"""
    p = _norm(phrase)
    if len(p) < 2:
        return
    for n in range(min(max_n, len(p)), 1, -1):
        for i in range(0, len(p) - n + 1):
            seg = p[i:i + n]
            if seg in _STOP_TERMS:
                continue
            if not re.search(r"[\u4e00-\u9fa5A-Za-z]", seg):
                continue
            yield seg


def feature_match(reb_f: ReimburseFeatures,
                  inv_f: InvoiceFeatures) -> Tuple[float, List[str]]:
    """文本特征碰撞打分（0~1）与命中的词条（用于前端 ✅ 展示）。

    规则：
    - 报销单词条整体出现在票面文字中，按词条长度给分（>=3 字 1.0，2 字 0.8）；
    - 整体不命中时退化为最长公共子串（4/3/2 字），命中「盈创」也能得高分；
    - 车牌号双方一致额外加分；
    - 都没有命中时，用整体相似度兜底（弱信号 0.3~0.5）。
    """
    inv_text = _norm(inv_f.text)
    if not inv_text:
        return 0.0, []
    best_len, hits = 0, []
    for phrase in reb_f.phrases:
        np = _norm(phrase)
        if len(np) >= 2 and np in inv_text:
            hits.append(phrase)
            best_len = max(best_len, len(np))
            continue
        for seg in _ngrams(phrase):
            if seg in inv_text:
                hits.append(seg)
                best_len = max(best_len, len(seg))
                break
    # 车牌号一致：强特征
    if reb_f.plate_no and inv_f.plate_no \
            and _norm(reb_f.plate_no) == _norm(inv_f.plate_no):
        hits.append(f"车牌号 {inv_f.plate_no}")
        best_len = max(best_len, 5)
    # 去重：被更长命中词包含的短词不再展示
    hits = sorted(dict.fromkeys(hits), key=len, reverse=True)
    shown: List[str] = []
    for h in hits:
        if not any(_norm(h) in _norm(s) for s in shown):
            shown.append(h)
    shown = shown[:3]

    if best_len >= 3:
        return 1.0, shown
    if best_len == 2:
        return 0.8, shown
    # 弱相似度兜底：把报销单特征与票面整体做序列比
    import difflib
    reb_text = _norm("".join(reb_f.phrases))
    if not reb_text:
        return 0.0, []
    ratio = difflib.SequenceMatcher(None, reb_text, inv_text).ratio()
    if ratio >= 0.55:
        return 0.5, shown
    if ratio >= 0.35:
        return 0.3, shown
    return 0.0, []
