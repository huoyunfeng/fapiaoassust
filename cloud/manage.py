"""云端订单 / 黑名单 / 解锁码管理 CLI（线下收单后录入订单号）。

套餐参数支持 SKU（推荐）或历史套餐名：
    basic_lifetime / ent_lifetime / maintain_basic / maintain_ent / upgrade_basic_to_ent
    personal（历史年费，等同 basic 年费）/ enterprise（历史年费）

示例：
    python cloud/manage.py add-order ORD-20261105-001 ent_lifetime
    python cloud/manage.py add-order basic_lifetime        # 省略订单号时自动生成
    python cloud/manage.py list
    python cloud/manage.py force-issue 1111-2222-3333-4444 ent_lifetime   # 紧急补发（无视支付状态）
    python cloud/manage.py blacklist 1111-2222-3333-4444 违规退款
    python cloud/manage.py unblacklist 1111-2222-3333-4444
    python cloud/manage.py reset-order ORD-20261105-001
    python cloud/manage.py unlock 1111-2222-3333-4444 1          # 签发超管解锁码（1天有效）
    python cloud/manage.py gen-keys        # 重新生成密钥对（会打印新公私钥）
"""
import json
import os
import secrets
import sys
import time
from pathlib import Path

# 与 license_server 保持同一数据目录（云端可用 CLOUD_DATA_DIR 覆盖）
DATA = Path(os.getenv("CLOUD_DATA_DIR", str(Path(__file__).parent)))


def _load(name: str, key: str) -> dict:
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8")).get(key, {})
    except Exception:
        return {}


def _save(name: str, key: str, data: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / name).write_text(json.dumps({key: data}, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def _resolve_target(target: str, days: int = 0) -> dict:
    """把 CLI 套餐参数归一化为订单字段：SKU 优先，兼容历史年费套餐名。"""
    from license_server import SKU_META

    target = (target or "").strip()
    if target in SKU_META:
        meta = SKU_META[target]
        return {"sku": target, "plan": meta["plan"], "kind": meta["kind"],
                "days": int(days or meta["days"] or 0)}
    if target in ("basic", "personal", "enterprise"):
        plan = "basic" if target == "personal" else target
        # 历史年费：不写 sku，云端按 annual 语义签发
        return {"plan": plan, "days": int(days or 365)}
    raise SystemExit("套餐只能是 SKU（basic_lifetime / ent_lifetime / maintain_basic / "
                     "maintain_ent / upgrade_basic_to_ent）或 personal / enterprise")


def add_order(order_id: str, target: str, days: int = 0) -> None:
    info = _resolve_target(target, days)
    orders = _load("orders.json", "orders")
    orders[order_id] = {
        **info, "status": "unused",
        "machine_code": None, "used_at": None,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save("orders.json", "orders", orders)
    label = info.get("sku") or f"{info['plan']} 年费"
    print(f"已录入订单 {order_id}（{label} / {info['days']} 天）")


def list_orders() -> None:
    orders = _load("orders.json", "orders")
    if not orders:
        print("暂无订单")
        return
    for oid, o in orders.items():
        print(f"{oid}\t{o.get('plan')}\t{o.get('status')}\t"
              f"{o.get('machine_code') or '-'}\t{o.get('used_at') or '-'}")


def set_blacklist(machine: str, reason: str, enabled: bool) -> None:
    data = _load("blacklist.json", "blacklist")
    if enabled:
        data[machine] = {"reason": reason, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        print(f"已拉黑 {machine}")
    else:
        data.pop(machine, None)
        print(f"已解除黑名单 {machine}")
    _save("blacklist.json", "blacklist", data)


def reset_order(order_id: str) -> None:
    orders = _load("orders.json", "orders")
    if order_id not in orders:
        raise SystemExit("订单不存在")
    orders[order_id].update({"status": "unused", "machine_code": None, "used_at": None})
    _save("orders.json", "orders", orders)
    devices = _load("devices.json", "devices")
    for m in [k for k in devices if devices[k].get("order_id") == order_id]:
        devices.pop(m, None)
    _save("devices.json", "devices", devices)
    print(f"订单 {order_id} 已解绑")


def unlock_code(machine: str, days: int = 1) -> None:
    """客服签发超管解锁码（短期有效，绑定机器码）。

    用户拿到解锁码后，在本地 /recover 页面输入即可重置管理员密码。
    """
    machine = (machine or "").strip()
    if not machine:
        raise SystemExit("机器码不能为空")
    days = max(1, int(days))
    from license_server import sign_payload

    now = int(time.time())
    payload = {
        "type": "unlock",
        "machine_code": machine,
        "exp": now + days * 86400,
        "issued_at": now,
    }
    code = sign_payload(payload)
    exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(payload["exp"]))
    print(f"已签发超管解锁码（{days} 天有效，到期 {exp_str}）")
    print(f"绑定机器码：{machine}")
    print("解锁码：")
    print(code)


def gen_keys() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    k = Ed25519PrivateKey.generate()
    print("私钥（仅云端）：")
    print(k.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode())
    print("公钥（客户端 config.py 内置）：")
    print(k.public_key().public_bytes(serialization.Encoding.PEM,
                                      serialization.PublicFormat.SubjectPublicKeyInfo).decode())


def force_issue(machine: str, target: str = "ent_lifetime", days: int = 0,
                core_fp: str = "") -> None:
    """客服救场：无视支付状态，直接为该机器码签发授权码并在终端打印。

    复用 license_server._issue_license，保证补发码与正常购买码的继承/叠加规则完全一致。
    同时写一条 paid 订单（订单号 FORCE-...），客户之后可用后 6 位自助找回。
    """
    machine = (machine or "").strip()
    if not machine:
        raise SystemExit("机器码不能为空")
    info = _resolve_target(target, days)
    # 复用云端签名与叠加实现（需要与 license_server 同目录、且私钥可用）
    from license_server import _issue_license

    now = int(time.time())
    order_id = f"FORCE-{time.strftime('%Y%m%d%H%M%S')}"
    order = {
        **info, "status": "paid", "machine_code": machine,
        "core_fp": (core_fp or "").strip(), "amount": 0,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "paid_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "activated_at": now, "source": "force-issue",
    }
    try:
        code, exp = _issue_license(order, order_id)
    except Exception as e:  # HTTPException 等：直接告知客服原因
        raise SystemExit(f"签发失败：{getattr(e, 'detail', e)}")

    orders = _load("orders.json", "orders")
    orders[order_id] = {**order, "trade_order_id": order_id,
                        "license_key": code, "exp": exp}
    _save("orders.json", "orders", orders)

    label = info.get("sku") or f"{info['plan']} 年费"
    exp_text = "永久" if order.get("lifetime") else time.strftime(
        "%Y-%m-%d", time.localtime(exp))
    print(f"已强制签发（{label}）")
    print(f"订单号：{order_id}")
    print(f"机器码：{machine}")
    print(f"授权到期：{exp_text}")
    print("授权码：")
    print(code)


def _is_target(value: str) -> bool:
    from license_server import SKU_META

    return value in SKU_META or value in ("basic", "personal", "enterprise")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "add-order":
        args = sys.argv[2:]
        if args and _is_target(args[0]):
            # 省略订单号：自动生成随机订单号（ORD-日期-随机后缀）
            order_id = f"ORD-{time.strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
            target = args[0]
            days = int(args[1]) if len(args) > 1 else 0
        elif len(args) >= 2:
            order_id, target = args[0], args[1]
            days = int(args[2]) if len(args) > 2 else 0
        else:
            print(__doc__)
            return
        add_order(order_id, target, days)
    elif cmd == "list":
        list_orders()
    elif cmd == "force-issue" and len(sys.argv) >= 3:
        machine = sys.argv[2]
        rest = sys.argv[3:]
        target, days = "ent_lifetime", 0
        if rest and _is_target(rest[0]):
            target = rest[0]
            rest = rest[1:]
        if rest and rest[0].isdigit():
            days = int(rest[0])
            rest = rest[1:]
        force_issue(machine, target, days, rest[0] if rest else "")
    elif cmd == "blacklist" and len(sys.argv) >= 3:
        set_blacklist(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "", True)
    elif cmd == "unblacklist" and len(sys.argv) >= 3:
        set_blacklist(sys.argv[2], "", False)
    elif cmd == "reset-order" and len(sys.argv) >= 3:
        reset_order(sys.argv[2])
    elif cmd == "unlock" and len(sys.argv) >= 3:
        machine = sys.argv[2]
        days = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        unlock_code(machine, days)
    elif cmd == "gen-keys":
        gen_keys()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
