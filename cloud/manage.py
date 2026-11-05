"""云端订单 / 黑名单管理 CLI（线下收单后录入订单号）。

示例：
    python cloud/manage.py add-order ORD-20261105-001 enterprise 365
    python cloud/manage.py add-order enterprise 365   # 省略订单号时自动生成随机订单号
    python cloud/manage.py list
    python cloud/manage.py blacklist 1111-2222-3333-4444 违规退款
    python cloud/manage.py unblacklist 1111-2222-3333-4444
    python cloud/manage.py reset-order ORD-20261105-001
    python cloud/manage.py gen-keys        # 重新生成密钥对（会打印新公私钥）
"""
import json
import secrets
import sys
import time
from pathlib import Path

DATA = Path(__file__).parent


def _load(name: str, key: str) -> dict:
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8")).get(key, {})
    except Exception:
        return {}


def _save(name: str, key: str, data: dict) -> None:
    (DATA / name).write_text(json.dumps({key: data}, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def add_order(order_id: str, plan: str, days: int = 365) -> None:
    if plan not in ("personal", "enterprise"):
        raise SystemExit("plan 只能是 personal / enterprise")
    orders = _load("orders.json", "orders")
    orders[order_id] = {
        "plan": plan, "days": int(days), "status": "unused",
        "machine_code": None, "used_at": None,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save("orders.json", "orders", orders)
    print(f"已录入订单 {order_id}（{plan} / {days} 天）")


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


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "add-order":
        args = sys.argv[2:]
        if args and args[0] in ("personal", "enterprise"):
            # 省略订单号：自动生成随机订单号（ORD-日期-随机后缀）
            order_id = f"ORD-{time.strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
            plan = args[0]
            days = int(args[1]) if len(args) > 1 else 365
        elif len(args) >= 2:
            order_id, plan = args[0], args[1]
            days = int(args[2]) if len(args) > 2 else 365
        else:
            print(__doc__)
            return
        add_order(order_id, plan, days)
    elif cmd == "list":
        list_orders()
    elif cmd == "blacklist" and len(sys.argv) >= 3:
        set_blacklist(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "", True)
    elif cmd == "unblacklist" and len(sys.argv) >= 3:
        set_blacklist(sys.argv[2], "", False)
    elif cmd == "reset-order" and len(sys.argv) >= 3:
        reset_order(sys.argv[2])
    elif cmd == "gen-keys":
        gen_keys()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
