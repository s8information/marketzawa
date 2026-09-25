#!/usr/bin/env python3
"""
pw.py — 今月（や指定月）のパスワードを表示する。noteメンバー向けに貼る用。

前提: 環境変数 MARKETZAWA_MASTER_KEY に、GitHub Secrets と同じ値を入れておく。
  export MARKETZAWA_MASTER_KEY='...ここに同じマスター鍵...'

使い方:
  python pw.py            # 今月
  python pw.py 2026-10    # 指定月（先出ししたいとき）
  python pw.py next       # 来月
"""
import os
import sys
import hmac
import base64
import hashlib
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))


def monthly_password(master_key: str, ym: str) -> str:
    digest = hmac.new(master_key.encode(), ym.encode(), hashlib.sha256).digest()
    tail = base64.b32encode(digest).decode().lower().replace("=", "")[:6]
    return f"zawa-{tail}"


def main():
    master = os.environ.get("MARKETZAWA_MASTER_KEY")
    if not master:
        sys.exit("環境変数 MARKETZAWA_MASTER_KEY を設定してください（GitHub Secretsと同じ値）")

    now = datetime.now(JST)
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "next":
        ym = (now.replace(day=1) + timedelta(days=32)).strftime("%Y-%m")
    elif arg:
        ym = arg
    else:
        ym = now.strftime("%Y-%m")

    print(f"{ym} のパスワード: {monthly_password(master, ym)}")


if __name__ == "__main__":
    main()
