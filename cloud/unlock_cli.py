"""超管解锁码签发 CLI（本地运行，通过 API 调用云端）。

用法：
    python cloud/unlock_cli.py --server http://服务器IP --token 你的TOKEN --machine 8E3F-8BBF-8148-F9BA --days 1

    # 交互模式（不带参数时进入）
    python cloud/unlock_cli.py
"""
import argparse
import getpass
import json
import sys
import urllib.request


def issue_unlock(server: str, token: str, machine: str, days: int) -> dict:
    """调用云端 API 签发解锁码。"""
    url = f"{server.rstrip('/')}/api/unlock_code"
    payload = json.dumps({"machine_code": machine, "days": days}).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json", "X-Admin-Token": token},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def main():
    parser = argparse.ArgumentParser(description="超管解锁码签发工具")
    parser.add_argument("--server", help="云端服务器地址，如 http://123.45.67.89")
    parser.add_argument("--token", help="管理员 Token（CLOUD_ADMIN_TOKEN）")
    parser.add_argument("--machine", help="用户机器码")
    parser.add_argument("--days", type=int, default=1, help="有效期天数（默认 1）")
    args = parser.parse_args()

    # 交互模式：缺省参数时提示输入
    server = args.server or input("云端服务器地址（如 http://123.45.67.89）: ").strip()
    token = args.token or getpass.getpass("管理员 Token: ").strip()
    machine = args.machine or input("用户机器码: ").strip()
    days = args.days

    if not server or not token or not machine:
        print("错误：服务器地址、Token、机器码都不能为空")
        sys.exit(1)

    print(f"\n正在签发解锁码（{days} 天有效）...")
    try:
        result = issue_unlock(server, token, machine, days)
    except Exception as e:
        print(f"签发失败：{e}")
        sys.exit(1)

    if result.get("ok"):
        import time
        exp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(result["exp"]))
        print(f"\n✅ 签发成功！有效期至 {exp}")
        print(f"绑定机器码：{machine}")
        print("=" * 60)
        print("解锁码（复制发给用户）：")
        print("=" * 60)
        print(result["unlock_code"])
        print("=" * 60)
        # 自动复制到剪贴板（Windows）
        try:
            import subprocess
            subprocess.run(["clip"], input=result["unlock_code"].encode(), check=True)
            print("\n✅ 解锁码已复制到剪贴板")
        except Exception:
            pass
    else:
        print(f"\n❌ 签发失败：{result.get('detail') or result.get('message') or result}")
        sys.exit(1)


if __name__ == "__main__":
    main()
