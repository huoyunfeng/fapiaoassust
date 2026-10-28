"""费用分类字典与匹配冲突仲裁（模块一 / 模块四）。

- 字典：分类只能禁用、不能物理删除；新增时对高度相似词给出合并提示。
- 冲突：匹配成功后校验发票侧（人工/记忆已确认）与报销单侧分类，
  不一致时落 category_conflict，由财务选择以谁为准或保留差异。
"""
from typing import Dict, List, Optional

from app.config import settings
from app.domain.categorize import STATUS_CONFIRMED, normalize_category, similar_categories
from app.repository import db
from app.repository.repos import (category_dict_repo, category_repo, conflict_repo,
                                 invoice_repo, match_repo, reimburse_category_repo,
                                 reimburse_repo)
from app.service import audit_service


# ---------------------------------------------------------------- 字典维护
def list_categories(include_disabled: bool = True) -> List[dict]:
    with db.read() as conn:
        return category_dict_repo.list(conn, enabled_only=not include_disabled)


def enabled_names() -> List[str]:
    with db.read() as conn:
        return category_dict_repo.names(conn, enabled_only=True)


def all_names(conn=None) -> List[str]:
    """全部分类名（含禁用）：校验历史值、判断相似度时使用。"""
    if conn is not None:
        return category_dict_repo.names(conn, enabled_only=False)
    with db.read() as c:
        return category_dict_repo.names(c, enabled_only=False)


def add_category(name: str, merge_into: str = "", force: bool = False,
                 operator: str = None) -> dict:
    """新增自定义分类；与已有分类高度相似时先返回 need_confirm 提示合并。"""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "message": "分类名称不能为空"}
    if len(name) > 20:
        return {"ok": False, "message": "分类名称过长（最多 20 个字符）"}
    with db.tx() as conn:
        existing = category_dict_repo.get_by_name(conn, name)
        if existing:
            if not existing["enabled"]:
                category_dict_repo.set_enabled(conn, existing["id"], True)
                audit_service.log(conn, "category_dict_enable", "category_dict",
                                  existing["id"], name, "禁用", "启用", operator)
                return {"ok": True, "id": existing["id"],
                        "message": "分类「%s」原为禁用状态，已重新启用" % name}
            return {"ok": False, "message": "分类「%s」已存在" % name}

        if merge_into:
            target = category_dict_repo.get_by_name(conn, merge_into)
            if target is None:
                return {"ok": False, "message": "合并目标分类「%s」不存在" % merge_into}
            moved = category_repo.rename_values(conn, name, merge_into)
            audit_service.log(conn, "category_dict_merge", "category_dict", "",
                              name, name, merge_into,
                              operator, "合并到「%s」，同步历史记录 %d 条" % (merge_into, moved))
            return {"ok": True, "message": "已合并到「%s」，同步更新 %d 条历史记录"
                                           % (merge_into, moved)}

        names = category_dict_repo.names(conn, enabled_only=False)
        sim = similar_categories(name, names)
        if sim and not force:
            return {"ok": False, "need_confirm": True, "similar": sim,
                    "message": "「%s」与已有分类 %s 高度相似，建议合并"
                               % (name, "、".join("「%s」" % s for s in sim))}

        cid = category_dict_repo.insert(conn, name, 0, category_dict_repo.max_sort(conn) + 1)
        audit_service.log(conn, "category_dict_add", "category_dict", cid,
                          name, "", name, operator)
        return {"ok": True, "id": cid, "message": "已新增分类「%s」" % name}


def toggle_category(cid: int, operator: str = None) -> dict:
    """启用/禁用分类（禁止物理删除）。系统预设分类强制锁定，不可禁用。"""
    with db.tx() as conn:
        c = category_dict_repo.get(conn, cid)
        if c is None:
            return {"ok": False, "message": "分类不存在"}
        if c["is_builtin"]:
            return {"ok": False,
                    "message": "系统预设分类「%s」已锁定，不允许禁用" % c["name"]}
        new_enabled = not bool(c["enabled"])
        category_dict_repo.set_enabled(conn, cid, new_enabled)
        audit_service.log(conn, "category_dict_enable" if new_enabled
                          else "category_dict_disable", "category_dict", cid, c["name"],
                          "启用" if c["enabled"] else "禁用",
                          "启用" if new_enabled else "禁用", operator)
        return {"ok": True, "enabled": new_enabled,
                "message": "分类「%s」已%s" % (c["name"], "启用" if new_enabled else "禁用")}


def rename_category(cid: int, new_name: str, force: bool = False,
                    operator: str = None) -> dict:
    """重命名自定义分类：仅限未被任何历史数据使用的分类（解决刚建完打错字）。

    已被使用时拒绝，应改用【合并】流程，保证历史账目不被直接改写。
    """
    new_name = (new_name or "").strip()
    if not new_name:
        return {"ok": False, "message": "新分类名称不能为空"}
    if len(new_name) > 20:
        return {"ok": False, "message": "分类名称过长（最多 20 个字符）"}
    with db.tx() as conn:
        c = category_dict_repo.get(conn, cid)
        if c is None:
            return {"ok": False, "message": "分类不存在"}
        if c["is_builtin"]:
            return {"ok": False,
                    "message": "系统预设分类「%s」已锁定，不允许重命名" % c["name"]}
        old_name = c["name"]
        if new_name == old_name:
            return {"ok": False, "message": "新名称与原名称相同"}
        if category_dict_repo.get_by_name(conn, new_name):
            return {"ok": False, "message": "分类「%s」已存在，请改用【合并】" % new_name}
        used = category_repo.usage_count(conn, old_name)
        if used > 0:
            return {"ok": False, "used": used,
                    "message": "分类「%s」已被 %d 条历史数据使用，不允许直接重命名，"
                               "请改用【合并】功能" % (old_name, used)}
        names = [n for n in category_dict_repo.names(conn, enabled_only=False)
                 if n != old_name]
        sim = similar_categories(new_name, names)
        if sim and not force:
            return {"ok": False, "need_confirm": True, "similar": sim,
                    "message": "「%s」与已有分类 %s 高度相似，建议改用【合并】"
                               % (new_name, "、".join("「%s」" % s for s in sim))}
        category_dict_repo.rename(conn, cid, new_name)
        audit_service.log(conn, "category_dict_rename", "category_dict", cid,
                          new_name, old_name, new_name, operator,
                          "未使用的自定义分类直接重命名")
        return {"ok": True, "message": "已将分类「%s」重命名为「%s」" % (old_name, new_name)}


def merge_category(cid: int, target_id: int, operator: str = None) -> dict:
    """合并自定义分类：历史数据全部改为目标分类，源分类随后禁用（保留备查）。

    目标可以是系统预设分类或其他自定义分类；源分类只能是自定义分类。
    """
    with db.tx() as conn:
        src = category_dict_repo.get(conn, cid)
        tgt = category_dict_repo.get(conn, target_id)
        if src is None:
            return {"ok": False, "message": "源分类不存在"}
        if tgt is None:
            return {"ok": False, "message": "目标分类不存在"}
        if src["is_builtin"]:
            return {"ok": False,
                    "message": "系统预设分类「%s」已锁定，不允许合并" % src["name"]}
        if src["id"] == tgt["id"]:
            return {"ok": False, "message": "不能与自身合并"}
        if not tgt["enabled"]:
            return {"ok": False,
                    "message": "目标分类「%s」已禁用，请先启用或另选目标" % tgt["name"]}
        moved = category_repo.rename_values(conn, src["name"], tgt["name"])
        category_dict_repo.set_enabled(conn, src["id"], False)
        audit_service.log(conn, "category_dict_merge", "category_dict", src["id"],
                          src["name"], src["name"], tgt["name"], operator,
                          "合并到「%s」，同步历史记录 %d 条，源分类已禁用"
                          % (tgt["name"], moved))
        return {"ok": True, "moved": moved,
                "message": "已将「%s」合并到「%s」（同步 %d 条历史记录），源分类已禁用"
                           % (src["name"], tgt["name"], moved)}


# ---------------------------------------------------------------- 双方分类口径
def reimburse_explicit(conn, reb_row: dict) -> tuple:
    """报销单侧显式分类：人工修改 > 报销单填写的费用类型。返回 (category, source)。"""
    rc = reimburse_category_repo.get(conn, reb_row["id"])
    names = category_dict_repo.names(conn, enabled_only=False)
    if rc and (rc.get("category") or "").strip():
        return normalize_category(rc["category"], names), "manual"
    exp = (reb_row.get("expense_type") or "").strip()
    if exp:
        return normalize_category(exp, names), "reimburse"
    return "", ""


def invoice_confirmed_map(conn, invoice_ids: List[int]) -> Dict[int, str]:
    """发票侧已确认分类（人工 / 纠正记忆），用于解绑等审计留痕。"""
    if not invoice_ids:
        return {}
    from app.service import invoice_service   # 惰性导入，避免循环依赖
    decisions = invoice_service.resolve_categories(conn, invoice_ids)
    return {iid: d["category"] for iid, d in decisions.items()
            if d.get("status") == STATUS_CONFIRMED
            and d.get("source") in ("manual", "memory")}


def invoice_effective_map(conn, invoice_ids: List[int]) -> Dict[int, str]:
    """发票侧展示口径的分类（人工 > 报销单类型 > 记忆 > 推测），冲突比对以此为准。"""
    if not invoice_ids:
        return {}
    from app.service import invoice_service
    decisions = invoice_service.resolve_categories(conn, invoice_ids)
    return {iid: d["category"] for iid, d in decisions.items()
            if (d.get("category") or "").strip()}


# ---------------------------------------------------------------- 冲突检测/仲裁
def detect_conflict(conn, reimburse_id: int) -> Optional[dict]:
    """匹配成功后校验双方分类：一致/单边为空 → 无冲突；不一致 → 落冲突记录。"""
    reb = reimburse_repo.get(conn, reimburse_id)
    if reb is None:
        conflict_repo.delete(conn, reimburse_id)
        return None
    ids = match_repo.matched_invoice_ids(conn, [reimburse_id]).get(reimburse_id) or []
    if not ids:
        conflict_repo.delete(conn, reimburse_id)
        return None
    reb_cat, _ = reimburse_explicit(conn, reb)
    if not reb_cat:
        conflict_repo.delete(conn, reimburse_id)
        return None
    inv_map = invoice_effective_map(conn, ids)
    if not inv_map:
        conflict_repo.delete(conn, reimburse_id)
        return None

    cats = sorted(set(inv_map.values()))
    detail = ""
    if len(ids) > 1:
        if len(cats) > 1:
            detail = ("一对多匹配且发票分类不一致（%s），报销单为「%s」，"
                      "差异允许保留，也可一键拉平（以报销单为准）"
                      % ("、".join(cats), reb_cat))
        elif cats[0] != reb_cat:
            detail = ("一对多匹配：%d 张发票分类为「%s」，报销单为「%s」"
                      % (len(ids), cats[0], reb_cat))
    elif cats[0] != reb_cat:
        detail = "发票分类「%s」与报销单分类「%s」不一致" % (cats[0], reb_cat)

    if not detail:
        conflict_repo.delete(conn, reimburse_id)
        return None
    conflict_repo.upsert(conn, reimburse_id, cats[0], reb_cat, detail)
    return conflict_repo.get(conn, reimburse_id)


def clear_conflict(conn, reimburse_id: int) -> None:
    conflict_repo.delete(conn, reimburse_id)


def resolve_conflict(reimburse_id: int, mode: str, operator: str = None) -> dict:
    """冲突仲裁：reimburse=以报销单为准 / invoice=以发票为准 / keep=保留差异。"""
    if mode not in ("reimburse", "invoice", "keep"):
        return {"ok": False, "message": "无效的仲裁方式"}
    operator = operator or settings.default_operator
    with db.tx() as conn:
        cf = conflict_repo.get(conn, reimburse_id)
        if cf is None:
            return {"ok": False, "message": "该报销单当前没有分类差异"}
        reb = reimburse_repo.get(conn, reimburse_id)
        ids = match_repo.matched_invoice_ids(conn, [reimburse_id]).get(reimburse_id) or []
        if reb is None or not ids:
            conflict_repo.delete(conn, reimburse_id)
            return {"ok": False, "message": "匹配关系已不存在，冲突记录已清理"}

        # 一对多也允许「以报销单为准」——即匹配中心的【一键拉平（强制拉平明细）】
        inv_map = invoice_effective_map(conn, ids)

        if mode == "keep":
            conflict_repo.set_resolution(conn, reimburse_id, "keep")
            audit_service.log(conn, "conflict_keep", "category_conflict", reimburse_id,
                              reb["reimburse_no"],
                              "发票「%s」/ 报销单「%s」" % (cf["invoice_category"],
                                                        cf["reimburse_category"]),
                              "保留差异", operator, cf["detail"])
            return {"ok": True, "message": "已保留差异（两边分类保持不变）"}

        if mode == "reimburse":
            reb_cat, _ = reimburse_explicit(conn, reb)
            if not reb_cat:
                return {"ok": False, "message": "报销单没有分类，无法以报销单为准"}
            from app.service import invoice_service
            for iid in ids:
                res = invoice_service._apply_manual_category(conn, iid, reb_cat)
                if not res.get("ok"):
                    raise RuntimeError(res.get("message") or "同步发票分类失败")
            conflict_repo.delete(conn, reimburse_id)
            audit_service.log(conn, "conflict_reimburse", "category_conflict", reimburse_id,
                              reb["reimburse_no"],
                              "发票「%s」" % cf["invoice_category"],
                              "报销单「%s」（同步 %d 张发票）" % (reb_cat, len(ids)),
                              operator, cf["detail"])
            return {"ok": True, "message": "已以报销单为准，同步 %d 张发票分类为「%s」"
                                           % (len(ids), reb_cat)}

        # 以发票为准
        cats = set(inv_map.values())
        if not cats:
            return {"ok": False, "message": "发票侧尚无已确认分类，无法以发票为准"}
        inv_cat = sorted(cats)[0]
        reimburse_category_repo.upsert(conn, reimburse_id, inv_cat, "arbitrate")
        conflict_repo.delete(conn, reimburse_id)
        audit_service.log(conn, "conflict_invoice", "category_conflict", reimburse_id,
                          reb["reimburse_no"],
                          "报销单「%s」" % cf["reimburse_category"],
                          "发票「%s」" % inv_cat, operator, cf["detail"])
        return {"ok": True, "message": "已以发票为准，报销单分类更新为「%s」" % inv_cat}


def count_unresolved() -> int:
    with db.read() as conn:
        return conflict_repo.count_unresolved(conn)


def list_conflicts(include_resolved: bool = True) -> List[dict]:
    """冲突列表（匹配中心展示用）。"""
    with db.read() as conn:
        cfs = conflict_repo.all(conn)
        reb_rows = {r["id"]: r for r in reimburse_repo.list(conn, limit=100000)}
        out = []
        for rid, cf in cfs.items():
            reb = reb_rows.get(rid)
            if reb is None:
                continue
            if not include_resolved and cf["resolution"]:
                continue
            ids = match_repo.matched_invoice_ids(conn, [rid]).get(rid) or []
            invs = [invoice_repo.get(conn, i) for i in ids[:5]]
            out.append({**cf,
                        "reimburse_no": reb["reimburse_no"],
                        "submitter": reb.get("submitter") or "",
                        "invoices": [i for i in invs if i],
                        "invoice_count": len(ids),
                        "multi": len(ids) > 1})
    out.sort(key=lambda x: (x["resolution"] != "", x["reimburse_id"]))
    return out
