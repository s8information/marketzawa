# -*- coding: utf-8 -*-
"""
marketzawa_ai.py — AI解説の生成＋有料コンテンツの暗号化

generate.py から使う。役割は3つ:
  1. 各カードの 'ai'（AIの見立て）を Claude Haiku で埋める … fill_ai()
  2. 月替わりパスワードを決定的に生成する           … monthly_password()
  3. 有料コンテンツを、ブラウザ(WebCrypto)で復号できる形式で暗号化 … encrypt()

環境変数（GitHub Secrets）:
  ANTHROPIC_API_KEY      … Anthropic APIキー（無ければAIはスキップ＝ai欄は空のまま）
  MARKETZAWA_MASTER_KEY  … 月替わりパスワードの種（自分で決めた長い文字列。一度決めたら変えない）

依存: anthropic, cryptography
"""

import os
import base64
import hashlib
import hmac
from datetime import datetime, timezone, timedelta

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ---- 設定（index.html と一致させる項目に注意）-------------------------------
MODEL = "claude-haiku-4-5-20251001"   # 最新IDはコンソールで確認可
MAX_AI_CALLS = 40                     # 1回の実行でAIに投げる上限（暴走課金の保険）
PBKDF2_ITER = 200000                  # ★ index.html の PBKDF2_ITER と必ず同じ

JST = timezone(timedelta(hours=9))


# ---- 月替わりパスワード -----------------------------------------------------
def current_ym() -> str:
    """JSTの YYYY-MM。月初(JST 0:00)に切り替わる。"""
    return datetime.now(JST).strftime("%Y-%m")


def monthly_password(master_key: str | None = None, ym: str | None = None) -> str:
    """マスター鍵と年月から決定的に生成。generate.py と pw.py で同じ値になる。"""
    master_key = master_key or os.environ["MARKETZAWA_MASTER_KEY"]
    ym = ym or current_ym()
    digest = hmac.new(master_key.encode(), ym.encode(), hashlib.sha256).digest()
    tail = base64.b32encode(digest).decode().lower().replace("=", "")[:6]
    return f"zawa-{tail}"


# ---- 暗号化（ブラウザのWebCryptoで復号可能）--------------------------------
# 形式: base64( salt[16] + iv[12] + ciphertext+tag )
#   KDF   : PBKDF2-HMAC-SHA256, iterations=PBKDF2_ITER, keylen=32
#   Cipher: AES-256-GCM（tag 128bit を ciphertext 末尾に連結）
def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITER, 32)


def encrypt(password: str, plaintext: str) -> str:
    salt = os.urandom(16)
    iv = os.urandom(12)
    ct = AESGCM(_derive_key(password, salt)).encrypt(iv, plaintext.encode(), None)
    return base64.b64encode(salt + iv + ct).decode()


# ---- AI解説（各カードの 'ai' 欄）-------------------------------------------
_SYSTEM = (
    "あなたは相場の異常検知サイト『今日のざわつき』の解説担当。"
    "与えた指標データだけを根拠に『AIの見立て』を日本語で淡々と書く。"
    "制約: 2〜3文/約80〜140字。売買推奨・目標値・断定的予測は禁止"
    "（『〜の可能性』『過去には〜だった』まで）。"
    "最後は『で、どうする？』の視点で“次に確認すべき点”を一つだけ示す。"
    "指標にない情報を創作しない。見出し・箇条書きは使わず地の文で。"
)


def _prompt(card: dict) -> str:
    facts = card.get("facts") or []
    facts_txt = " / ".join(f"{k}:{v}" for k, v in facts) if facts else "なし"
    return (
        f"銘柄: {card.get('name')}\n"
        f"市場: {card.get('mkt')}\n"
        f"検知シグナル: {card.get('badge')}\n"
        f"当日の動き: {card.get('chg')}\n"
        f"背景データ: {facts_txt}\n"
        f"無料側の一言: {card.get('desc')}\n\n"
        "上記だけを根拠に、この銘柄の『AIの見立て』を書いて。"
    )


def generate_ai_comment(client, card: dict) -> str:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=_SYSTEM,
        messages=[{"role": "user", "content": _prompt(card)}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def fill_ai(cards: list[dict], *, max_calls: int = MAX_AI_CALLS) -> list[dict]:
    """cards の各要素の 'ai' を埋める。APIキーが無ければ何もしない（ai欄は空のまま）。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("!! ANTHROPIC_API_KEY 未設定：AIの見立てをスキップ（ai欄は空のまま）")
        return cards

    from anthropic import Anthropic  # 遅延import
    client = Anthropic()
    n = 0
    for c in cards:
        if n >= max_calls:
            print(f"[ai] MAX_AI_CALLS({max_calls})到達、以降スキップ")
            break
        try:
            c["ai"] = generate_ai_comment(client, c)
            n += 1
            print(f"[ai] {c.get('name')} OK")
        except Exception as e:
            c.setdefault("ai", "")
            print(f"[ai] {c.get('name')} 失敗: {e}")
    print(f"[ai] 生成 {n} 件")
    return cards


# ---- セクター/テーマ（グループ）のAI見立て ---------------------------------
_SYSTEM_GROUP = (
    "あなたは相場の異常検知サイト『今日のざわつき』の解説担当。"
    "与えた指標データだけを根拠に、そのセクター/テーマが今日なぜ動いたかの見立てを日本語で淡々と書く。"
    "制約: 2〜3文/約80〜140字。売買推奨・目標値・断定的予測は禁止"
    "（『〜の可能性』『過去には〜だった』まで）。"
    "最後は『で、どうする？』の視点で“次に確認すべき点”を一つだけ示す。"
    "指標にない情報を創作しない。見出し・箇条書きは使わず地の文で。"
)


def _prompt_group(item: dict, kind: str) -> str:
    facts = item.get("facts") or []
    facts_txt = " / ".join(f"{k}:{v}" for k, v in facts) if facts else "なし"
    return (
        f"{kind}: {item.get('name')}\n"
        f"ざわつきスコア: {item.get('score')}/100（{item.get('lv')}）\n"
        f"指標: {facts_txt}\n\n"
        f"この{kind}が今日ざわついている理由の見立てを書いて。"
    )


def generate_group_comment(client, item: dict, kind: str) -> str:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=_SYSTEM_GROUP,
        messages=[{"role": "user", "content": _prompt_group(item, kind)}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def fill_ai_groups(items, kind, *, score_threshold=50, max_calls=MAX_AI_CALLS):
    """
    セクター/テーマ各要素の 'ai' を埋める。
    score_threshold 未満（＝静かな群）はAIに投げない（ai欄は空のまま）。
    APIキーが無ければ何もしない。
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return items
    from anthropic import Anthropic
    client = Anthropic()
    n = 0
    for it in items:
        if it.get("score", 0) < score_threshold:
            continue
        if n >= max_calls:
            print(f"[ai/{kind}] 上限到達、以降スキップ")
            break
        try:
            it["ai"] = generate_group_comment(client, it, kind)
            n += 1
            print(f"[ai/{kind}] {it.get('name')} OK")
        except Exception as e:
            it.setdefault("ai", "")
            print(f"[ai/{kind}] {it.get('name')} 失敗: {e}")
    print(f"[ai/{kind}] 生成 {n} 件")
    return items


if __name__ == "__main__":
    # 暗号の自己テスト（APIは叩かない）
    pw = monthly_password("MASTER_SECRET_EXAMPLE", "2026-09")
    print("今月のパスワード例:", pw)
    import json
    blob = encrypt(pw, json.dumps({"cards": [{"name": "BTC", "ai": "テスト"}]}, ensure_ascii=False))
    print("暗号化blob(先頭):", blob[:48], "...")
