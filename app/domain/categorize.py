"""费用智能归类（纯函数）：报销内容 -> 标准财务分类。

规则：
- 第一层：实体品名词典（具体商品/服务名，歧义低，逐类目优先匹配）。
  通用关键词要求类目词字面出现（如"电器""酒店"），但实际发票常写具体品名：
  "美的电热水器"不含"电器"，"火锅"不含"餐"，"高铁票"不含"火车"，"奶茶"不含"茶饮"。
- 第二层：通用关键词，匹配顺序即优先级：餐饮 > 差旅 > 交通 > 办公 > 通讯。
  （"火车票"同时含"火车"与"车"，差旅必须排在交通之前，否则会被"车"吞掉。）
- 未命中任何规则 -> "其他费用"。
- 用户自定义词典（item_alias）在归类之前已把原始词替换为用户改名，
  因此用户改名的归类结果天然优先于本模块的内置规则。
"""

# 第一层：实体品名词典（按类目组织，顺序即优先级，差旅须在交通之前——"动车"含"车"）
ENTITY_RULES = (
    ("办公费", (
        "热水器", "冰箱", "空调", "洗衣机", "电视机", "电风扇", "风扇", "微波炉",
        "饮水机", "净化器", "取暖器", "电暖器", "加湿器", "吸尘器", "电饭煲",
        "电水壶", "热水壶", "电磁炉", "油烟机", "消毒柜", "烘干机", "洗碗机",
        "扫地机", "挂烫机", "电热毯", "吹风机", "投影仪", "打印机", "复印机",
        "碎纸机", "电脑", "显示器", "路由器", "交换机", "台灯", "插座", "排插",
    )),
    ("餐饮费", (
        "火锅", "烧烤", "烤肉", "奶茶", "咖啡", "外卖", "食堂", "酒水",
        "饮料", "饮品", "蛋糕", "水果", "茶叶", "月饼", "肯德基", "麦当劳",
    )),
    ("差旅费", (
        "高铁", "动车", "宾馆", "民宿", "客栈", "航班", "航空", "卧铺",
    )),
    ("交通费", (
        "地铁", "公交", "滴滴", "网约车", "打车", "ETC", "过路费", "轮渡",
    )),
    ("通讯费", (
        "流量", "电信", "联通", "手机充值",
    )),
)

# 第二层：通用关键词规则
CATEGORY_RULES = (
    ("餐饮费", ("餐", "饭", "食品", "茶饮")),
    ("差旅费", ("住宿", "酒店", "机票", "火车")),
    ("交通费", ("车", "加油", "高速", "交通")),
    ("办公费", ("文具", "耗材", "电器", "设备", "办公")),
    ("通讯费", ("话费", "宽带", "通信")),
)

DEFAULT_CATEGORY = "其他费用"

# 仪表盘/列表允许的标准分类（人工修改只能选这些，避免出现细碎发票明细名）
STANDARD_CATEGORIES = ("餐饮费", "交通费", "差旅费", "办公费", "通讯费", "其他费用")

STATUS_CONFIRMED = "CONFIRMED"   # 已确认（报销单类型 / 人工修正 / 记忆规则）
STATUS_PENDING = "PENDING"       # 待确认（系统推测，需财务核实）

MISMATCH_HINT = "发票内容与费用类型不匹配"


def categorize_item(name: str) -> str:
    """报销内容 -> 标准财务分类名称。"""
    s = (name or "").strip()
    # 实体品名优先：避免被通用关键词误吞（如 "电饭煲" 含 "饭"、"动车" 含 "车"）
    for category, keywords in ENTITY_RULES:
        if any(k in s for k in keywords):
            return category
    for category, keywords in CATEGORY_RULES:
        if any(k in s for k in keywords):
            return category
    return DEFAULT_CATEGORY


def normalize_category(text: str, valid=None) -> str:
    """把自由填写的费用类型收敛到字典分类（仪表盘只展示字典里的分类）。

    先做语义近似（如「差旅」→「差旅费」），再走发票明细词典；仍未命中的归入默认大类。
    valid: 当前可用的分类名列表（默认内置六类，设置页新增的自定义分类也参与收敛）。
    """
    s = (text or "").strip()
    if not s:
        return ""
    names = tuple(valid) if valid else STANDARD_CATEGORIES
    if s in names:
        return s
    for c in names:
        if c.startswith(s) or s in c:
            return c
    stem = _stem(s)
    for c in names:
        if _stem(c) == stem:
            return c
    return categorize_item(s)


def _stem(text: str) -> str:
    """去掉「费 / 费用 / 支出」等通用后缀，用于相似度判断（餐饮 ≈ 餐饮费）。"""
    s = (text or "").strip().replace(" ", "")
    for suf in ("费用", "支出", "费"):
        if len(s) > len(suf) and s.endswith(suf):
            return s[:-len(suf)]
    return s


def similar_categories(name: str, names) -> list:
    """返回与 name 高度相似的已有分类（用于字典新增时的「合并」提示）。"""
    s = (name or "").strip()
    if not s:
        return []
    stem = _stem(s)
    out = []
    for n in names:
        if n == s:
            continue
        if _stem(n) == stem or s in n or n in s:
            out.append(n)
    return out


def resolve_category(item_summary: str, expense_type: str = "",
                     submitter: str = "", manual_category: str = "",
                     item_memory: dict = None, submitter_memory: dict = None,
                     has_reimburse: bool = False,
                     reimburse_manual_category: str = "") -> dict:
    """费用分类决策：报销单（业务实质）> 模板 > 发票（税务凭证）。

    优先级：
    1. 人工修正（invoice_category）——最高优先，财务说了算
    2. 报销单分类：人工确认的报销单分类（reimburse_category）> 导入的费用类型
    3. 纠正记忆（该提交人 / 该发票明细 曾被财务改过）
    4. 发票明细智能推测（报销单类型为空或无报销单时兜底）

    返回 {"category", "status", "source", "suggested", "hint"}
    - status: CONFIRMED 已确认 / PENDING 待确认
    - source: manual 人工 / reimburse 报销单 / memory 记忆 / inferred 推测 / orphan 未报销推测
    - suggested: 系统按发票明细推测的分类（前端提示"系统推测为 X"用）
    - hint: 发票内容与报销单类型明显不符时的轻量提示（不拦截）
    """
    item_summary = (item_summary or "").strip()
    exp = (expense_type or "").strip()
    reb_manual = (reimburse_manual_category or "").strip()
    suggested = categorize_item(item_summary)

    def _out(category, status, source, hint="", raw=""):
        return {"category": category, "status": status, "source": source,
                "suggested": suggested, "hint": hint, "raw_category": raw}

    # 1. 人工修正：财务确认过的分类永久优先（已人工拍板，不再提示“不匹配”）
    if manual_category:
        raw = exp if exp and normalize_category(exp) != manual_category else ""
        return _out(manual_category, STATUS_CONFIRMED, "manual", "", raw)

    # 2. 报销单侧已有明确分类：人工确认的报销单分类 > 导入的费用类型
    if reb_manual or exp:
        chosen = reb_manual or exp
        mem = submitter_memory or {}
        if not reb_manual and mem.get("wrong_value") and mem.get("wrong_value") == exp:
            return _out(mem["category"], STATUS_CONFIRMED, "memory")
        # 自由填写的类型（如项目名/「差旅」）收敛到标准大类，避免图表出现细碎名
        cat = normalize_category(chosen)
        raw = exp if exp and cat != exp else ""
        # 追加校验：明细与填写类型明显不符时给轻量提示（不拦截）
        hint = "" if (suggested == cat or DEFAULT_CATEGORY in (suggested, cat)) else MISMATCH_HINT
        return _out(cat, STATUS_CONFIRMED, "reimburse", hint, raw)

    # 3. 报销单未填类型：明细记忆（同品名）优先；
    #    提交人记忆只在「当时也没填」时套用，避免把某员工的餐饮纠正套到他的所有发票上
    item_mem = item_memory or {}
    if item_mem.get("category"):
        return _out(item_mem["category"], STATUS_CONFIRMED, "memory")
    sub_mem = submitter_memory or {}
    if sub_mem.get("category") and not (sub_mem.get("wrong_value") or "").strip():
        return _out(sub_mem["category"], STATUS_CONFIRMED, "memory")

    # 4. 兜底：系统按发票明细推测，标记待确认（未报销的另标 orphan）
    source = "inferred" if has_reimburse else "orphan"
    return _out(suggested, STATUS_PENDING, source)
