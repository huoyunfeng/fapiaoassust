"""费用分类字典与匹配冲突仲裁（模块一 / 模块四）。

- 字典：分类只能禁用、不能物理删除；新增时对高度相似词给出合并提示。
- 冲突：匹配成功后校验发票侧（人工/记忆已确认）与报销单侧分类，
  不一致时落 category_conflict，由财务选择以谁为准或保留差异。
"""
from typing import Dict, List, Optional

from app.config import settings
from app.domain.categorize import known_category, similar_categories
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
            merge = category_repo.rename_values(conn, name, merge_into)
            moved = merge["moved"]
            defer_recheck(merge["reimburse_ids"])
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
        merge = category_repo.rename_values(conn, src["name"], tgt["name"])
        moved = merge["moved"]
        defer_recheck(merge["reimburse_ids"])
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
        return known_category(rc["category"], names) or rc["category"].strip(), \
            (rc.get("source") or "manual")
    exp = (reb_row.get("expense_type") or "").strip()
    if exp:
        return known_category(exp, names) or exp, "reimburse"
    return "", ""


def invoice_confirmed_map(conn, invoice_ids: List[int]) -> Dict[int, str]:
    """发票侧已确认分类（仅人工决策），用于解绑等审计留痕。"""
    if not invoice_ids:
        return {}
    dec = category_repo.decisions(conn, invoice_ids)
    return {iid: (d.get("category") or "").strip()
            for iid, d in dec.items() if (d.get("category") or "").strip()}


def invoice_manual_candidates(conn, invoice_ids: List[int]) -> List[dict]:
    """冲突仲裁候选：该组中「有人工分类决策」的发票及其分类（按 id 升序）。"""
    if not invoice_ids:
        return []
    dec = category_repo.decisions(conn, invoice_ids)
    out = []
    for iid in sorted(invoice_ids):
        d = dec.get(iid)
        if not d or not (d.get("category") or "").strip():
            continue
        row = invoice_repo.get(conn, iid) or {}
        out.append({"invoice_id": iid,
                    "invoice_no": row.get("invoice_no") or "",
                    "category": d["category"].strip(),
                    "version": int(d.get("version") or 0)})
    return out


# ---------------------------------------------------------------- 冲突检测/仲裁
def detect_conflict(conn, reimburse_id: int) -> Optional[dict]:
    """匹配成功后校验双方分类（只认原始人工字段，杜绝循环推导）：

    - 发票侧只读取 invoice_category（财务实际设置的分类）；
      系统推测、从报销单继承的展示值一律视为「未人工设置」→ 不构成冲突；
    - 报销单侧读取 reimburse_category > 导入的费用类型；
    - 一方为空或双方一致 → 无冲突；不一致 → 落冲突记录（含具体发票号）。
    """
    reb = reimburse_repo.get(conn, reimburse_id)
    if reb is None or reb.get("deleted_at"):
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

    names = category_dict_repo.names(conn, enabled_only=False)
    manual = {}
    for iid, d in category_repo.decisions(conn, ids).items():
        cat = (d.get("category") or "").strip()
        if cat:
            manual[iid] = known_category(cat, names) or cat
    if not manual:
        # 发票侧没有任何人工设置：纯系统推断不算冲突
        conflict_repo.delete(conn, reimburse_id)
        return None

    diffs = {iid: cat for iid, cat in manual.items() if cat != reb_cat}
    if not diffs:
        conflict_repo.delete(conn, reimburse_id)
        return None

    cat_list = sorted(set(manual.values()))
    detail = ""
    if len(ids) > 1:
        if len(cat_list) > 1:
            detail = ("一对多匹配且发票分类不一致（%s），报销单为「%s」，"
                      "差异允许保留，也可一键拉平（以报销单为准）"
                      % ("、".join(cat_list), reb_cat))
        else:
            detail = ("一对多匹配：%d 张发票分类为「%s」，报销单为「%s」"
                      % (len(ids), cat_list[0], reb_cat))
    else:
        detail = "发票分类「%s」与报销单分类「%s」不一致" % (cat_list[0], reb_cat)
    nos = []
    for iid in sorted(diffs):
        row = invoice_repo.get(conn, iid) or {}
        nos.append(row.get("invoice_no") or f"#{iid}")
    if nos:
        detail += "；差异发票：" + "、".join(nos)

    conflict_repo.upsert(conn, reimburse_id, cat_list[0], reb_cat, detail)
    return conflict_repo.get(conn, reimburse_id)


def inherit_reimburse_category(conn, reimburse_id: int) -> str:
    """主动匹配生效后持久化继承：报销单无分类且发票侧只有一个已确认分类时，

    写入 reimburse_category(source='linked')，前端据此显示「由关联发票继承而来」。
    多个不同分类时不写，交由冲突检测标记差异。
    """
    rc = reimburse_category_repo.get(conn, reimburse_id)
    if rc and (rc.get("category") or "").strip():
        return ""
    reb = reimburse_repo.get(conn, reimburse_id) or {}
    if reb.get("deleted_at"):
        return ""
    # 报销单自己已填写费用类型时不覆盖（业务实质优先），留给冲突检测按差异处理
    if (reb.get("expense_type") or "").strip():
        return ""
    ids = match_repo.matched_invoice_ids(conn, [reimburse_id]).get(reimburse_id) or []
    if not ids:
        return ""
    names = category_dict_repo.names(conn, enabled_only=False)
    cats = set()
    for d in category_repo.decisions(conn, ids).values():
        cat = (d.get("category") or "").strip()
        if cat:
            cats.add(known_category(cat, names) or cat)
    if len(cats) != 1:
        return ""
    cat = sorted(cats)[0]
    reimburse_category_repo.upsert(conn, reimburse_id, cat, "linked")
    return cat


def defer_recheck(reimburse_ids, inherit: bool = False) -> None:
    """在当前事务成功提交后统一重算；回滚时不产生任何冲突副作用。"""
    ids = sorted({int(rid) for rid in (reimburse_ids or []) if rid})
    if ids:
        db.after_commit(lambda: recheck_after_commit(ids, inherit=inherit))


def recheck_after_commit(reimburse_ids, inherit: bool = False) -> Dict[int, Optional[dict]]:
    """事务提交后的唯一冲突重算入口。

    调用方必须先结束自己的 db.tx()；本函数在新事务中按需持久化继承分类，
    再由 detect_conflict 统一生成、更新或清理冲突。业务代码不得单独删冲突。
    """
    ids = sorted({int(rid) for rid in (reimburse_ids or []) if rid})
    if not ids:
        return {}
    result = {}
    with db.tx() as conn:
        for rid in ids:
            if inherit:
                inherit_reimburse_category(conn, rid)
            result[rid] = detect_conflict(conn, rid)
    return result


def resolve_conflict(reimburse_id: int, mode: str, operator: str = None,
                     invoice_id: Optional[int] = None,
                     versions: Dict[int, int] = None,
                     reimburse_version: Optional[int] = None) -> dict:
    """冲突仲裁，复用分类写入服务；提交后由统一引擎重算剩余差异。"""
    if mode not in ("reimburse", "invoice", "keep"):
        return {"ok": False, "message": "无效的仲裁方式"}
    operator = operator or settings.default_operator
    versions = {int(k): int(v) for k, v in (versions or {}).items()}
    result = None
    needs_recheck = False
    from app.repository.repos import CategoryLockError
    from app.service import invoice_service
    try:
        with db.tx() as conn:
            cf = conflict_repo.get(conn, reimburse_id)
            if cf is None:
                return {"ok": False, "message": "该报销单当前没有分类差异"}
            reb = reimburse_repo.get(conn, reimburse_id)
            ids = match_repo.matched_invoice_ids(conn, [reimburse_id]).get(reimburse_id) or []
            if reb is None or reb.get("deleted_at") or not ids:
                result = {"ok": False, "message": "匹配关系已不存在，冲突记录已清理"}
                needs_recheck = True
            elif mode == "keep":
                conflict_repo.set_resolution(conn, reimburse_id, "keep")
                audit_service.log(conn, "conflict_keep", "category_conflict", reimburse_id,
                                  reb["reimburse_no"],
                                  "发票「%s」/ 报销单「%s」" % (cf["invoice_category"],
                                                            cf["reimburse_category"]),
                                  "保留差异", operator, cf["detail"])
                result = {"ok": True, "message": "已保留差异（两边分类保持不变）"}
                needs_recheck = True
            elif mode == "reimburse":
                reb_cat, _ = reimburse_explicit(conn, reb)
                if not reb_cat:
                    return {"ok": False, "message": "报销单没有分类，无法以报销单为准"}
                if reb_cat not in category_dict_repo.names(conn, enabled_only=False):
                    return {"ok": False,
                            "message": "报销单分类「%s」不在分类字典中，请先修正报销单分类" % reb_cat}
                decisions = category_repo.decisions(conn, ids)
                stale = [iid for iid in ids if iid in versions and
                         int((decisions.get(iid) or {}).get("version") or 0) != versions[iid]]
                if stale:
                    return {"ok": False, "stale": True, "stale_ids": stale,
                            "message": "数据已被他人修改，请刷新后重试"}
                for iid in ids:
                    res = invoice_service.apply_invoice_category_conn(
                        conn, iid, reb_cat, learn=False, sync_reimburse=False,
                        expected_version=versions.get(iid))
                    if not res.get("ok"):
                        raise RuntimeError(res.get("message") or "同步发票分类失败")
                audit_service.log(conn, "conflict_reimburse", "category_conflict", reimburse_id,
                                  reb["reimburse_no"], "发票「%s」" % cf["invoice_category"],
                                  "报销单「%s」（同步 %d 张发票）" % (reb_cat, len(ids)),
                                  operator, cf["detail"])
                result = {"ok": True, "message": "已以报销单为准，同步 %d 张发票分类为「%s」"
                                               % (len(ids), reb_cat)}
                needs_recheck = True
            else:
                candidates = invoice_manual_candidates(conn, ids)
                if not candidates:
                    return {"ok": False, "message": "发票侧尚无已确认分类，无法以发票为准"}
                if len(ids) > 1 and invoice_id is None:
                    return {"ok": False, "need_invoice": True, "invoices": candidates,
                            "message": "该报销单关联多张发票，请明确选择以哪张发票为准"}
                chosen = (next((c for c in candidates
                                if int(c["invoice_id"]) == int(invoice_id)), None)
                          if invoice_id is not None else candidates[0])
                if chosen is None:
                    return {"ok": False,
                            "message": "所选发票不属于该报销单，或该发票没有人工分类"}
                iid = int(chosen["invoice_id"])
                current_version = int((category_repo.get(conn, iid) or {}).get("version") or 0)
                if iid in versions and versions[iid] != current_version:
                    return {"ok": False, "stale": True, "stale_ids": [iid],
                            "message": "数据已被他人修改，请刷新后重试"}
                inv_cat = chosen["category"]
                res = invoice_service.apply_reimburse_category_conn(
                    conn, reimburse_id, inv_cat, operator,
                    expected_version=reimburse_version, sync_invoices=False,
                    source="arbitrate", audit=False)
                if not res.get("ok"):
                    return res
                audit_service.log(conn, "conflict_invoice", "category_conflict", reimburse_id,
                                  reb["reimburse_no"], "报销单「%s」" % cf["reimburse_category"],
                                  "发票「%s」（发票 %s）" % (inv_cat, chosen["invoice_no"] or iid),
                                  operator, cf["detail"])
                result = {"ok": True, "message": "已以发票 %s 为准，报销单分类更新为「%s」"
                                               % (chosen["invoice_no"] or ("#" + str(iid)), inv_cat)}
                needs_recheck = True
    except CategoryLockError:
        return {"ok": False, "stale": True, "message": "数据已被他人修改，请刷新后重试"}
    if needs_recheck:
        recheck_after_commit([reimburse_id])
    return result


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
            decisions = category_repo.decisions(conn, ids)
            rc = reimburse_category_repo.get(conn, rid) or {}
            out.append({**cf,
                        "reimburse_no": reb["reimburse_no"],
                        "submitter": reb.get("submitter") or "",
                        "invoices": [i for i in invs if i],
                        "invoice_categories": invoice_manual_candidates(conn, ids),
                        "invoice_versions": {str(iid): int((decisions.get(iid) or {}).get("version") or 0)
                                             for iid in ids},
                        "reimburse_version": int(rc.get("version") or 0),
                        "invoice_count": len(ids),
                        "multi": len(ids) > 1})
    out.sort(key=lambda x: (x["resolution"] != "", x["reimburse_id"]))
    return out
