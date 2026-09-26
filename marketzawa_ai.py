# -*- coding: utf-8 -*-
"""
marketzawa_ai.py — AI解説の生成＋有料コンテンツの暗号化

役割:
  1. カード/セクター/テーマ/金利の 'ai'（見立て）を Claude Haiku で埋める
  2. 月替わりパスワードを決定的に生成
  3. 有料コンテンツを WebCrypto で復号できる形式に暗号化

AIの見立ての質は「渡す材料の厚さ」で決まる。generate.py 側で
 本日の動き / 直近トレンド / 値位置 / 過去の類似局面のその後 / 市場全体の状況
を facts・hist・backdrop として渡し、ここで統合的に解釈させる。

環境変数(GitHub Secrets): ANTHROPIC_API_KEY, MARKETZAWA_MASTER_KEY
依存: anthropic, cryptography
"""

import os
import base64
import hashlib
import hmac
from datetime import datetime, timezone, timedelta

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MODEL = "claude-haiku-4-5-20251001"   # 最新IDはコンソールで確認可
MAX_AI_CALLS = 45                     # 1回の実行でAIに投げる上限（暴走課金の保険）
PBKDF2_ITER = 200000                  # ★ index.html の PBKDF2_ITER と必ず同じ
JST = timezone(timedelta(hours=9))


# ---- 月替わりパスワード -----------------------------------------------------
def current_ym() -> str:
    return datetime.now(JST).strftime("%Y-%m")


def monthly_password(master_key: str | None = None, ym: str | None = None) -> str:
    master_key = master_key or os.environ["MARKETZAWA_MASTER_KEY"]
    ym = ym or current_ym()
    digest = hmac.new(master_key.encode(), ym.encode(), hashlib.sha256).digest()
    tail = base64.b32encode(digest).decode().lower().replace("=", "")[:6]
    return f"zawa-{tail}"


# ---- 暗号化（WebCrypto互換）------------------------------------------------
def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITER, 32)


def encrypt(password: str, plaintext: str) -> str:
    salt = os.urandom(16)
    iv = os.urandom(12)
    ct = AESGCM(_derive_key(password, salt)).encrypt(iv, plaintext.encode(), None)
    return base64.b64encode(salt + iv + ct).decode()


# ---- 共通：見立ての作法 -----------------------------------------------------
_SYSTEM = (
    "あなたは株・為替・商品・暗号資産・金利を横断して見る相場解説の専門家。"
    "与えた材料（本日の動き／直近トレンド／値位置／過去の類似局面のその後／市場全体の状況）から、"
    "『この動きが何を意味するか』を一段踏み込んで言い切る。"
    "最も価値があるのは次の2点で、これを核に書く："
    "①過去の類似局面の実績（例『過去◯回中、5営業日後は上昇◯・下落◯』というベースレート）を"
    "そのまま解釈として提示する。"
    "②他資産・金利との間に食い違い/整合があれば指摘する"
    "（例『金利上昇はハイテクに逆風のはずが逆行高＝選別物色の兆候』）。"
    "厳禁：『〜を確認したい』『〜を見極めたい』『今後の推移を確認する必要がある』のような、"
    "判断を先送りする“確認待ち”の締め。この手の文は一切書かない。"
    "売買推奨・目標値・断定的予測も禁止（『過去は〜だった』『〜の傾向』まで）。"
    "本日の動きが小さく、過去類似の該当もなく、他資産との食い違いも無い場合は、"
    "無理に長文化せず『変動は通常の範囲内で、特筆すべき点はない』と一文で簡潔に終える。"
    "長さは内容量に応じて30〜180字で可変。見出し・箇条書きは使わず地の文で。"
)


def _facts_txt(item):
    facts = item.get("facts") or []
    return " / ".join(f"{k}:{v}" for k, v in facts) if facts else "特記なし"


def _context_block(item, backdrop):
    lines = [f"本日の指標: {_facts_txt(item)}"]
    if item.get("hist"):
        lines.append(f"過去の類似局面: {item['hist']}")
    if backdrop:
        lines.append(f"市場全体: {backdrop}")
    return "\n".join(lines)


# ---- カード（個別資産）-----------------------------------------------------
def _prompt(card, backdrop):
    return (
        f"対象: {card.get('name')}（{card.get('mkt')}） / 検知: {card.get('badge')}\n"
        f"{_context_block(card, backdrop)}\n\n"
        "上記の文脈をすべて踏まえ、この対象の『AIの見立て』を書いて。"
    )


def generate_ai_comment(client, card, backdrop):
    msg = client.messages.create(
        model=MODEL, max_tokens=500, system=_SYSTEM,
        messages=[{"role": "user", "content": _prompt(card, backdrop)}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def fill_ai(cards, *, backdrop="", max_calls=MAX_AI_CALLS):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("!! ANTHROPIC_API_KEY 未設定：AIの見立てをスキップ")
        return cards
    from anthropic import Anthropic
    client = Anthropic()
    n = 0
    for c in cards:
        if n >= max_calls:
            print(f"[ai] 上限{max_calls}到達、以降スキップ"); break
        try:
            c["ai"] = generate_ai_comment(client, c, backdrop); n += 1
            print(f"[ai] {c.get('name')} OK")
        except Exception as e:
            c.setdefault("ai", ""); print(f"[ai] {c.get('name')} 失敗: {e}")
    print(f"[ai] 生成 {n} 件")
    return cards


# ---- セクター/テーマ/金利（グループ）---------------------------------------
def _prompt_group(item, kind, backdrop):
    return (
        f"{kind}: {item.get('name')} / ざわつき {item.get('score')}/100（{item.get('lv')}）\n"
        f"{_context_block(item, backdrop)}\n\n"
        f"上記の文脈を踏まえ、この{kind}が今日動いた理由と示唆の『見立て』を書いて。"
    )


def generate_group_comment(client, item, kind, backdrop):
    msg = client.messages.create(
        model=MODEL, max_tokens=500, system=_SYSTEM,
        messages=[{"role": "user", "content": _prompt_group(item, kind, backdrop)}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def fill_ai_groups(items, kind, *, backdrop="", score_threshold=50, max_calls=MAX_AI_CALLS):
    """score_threshold 未満（静かな群）はAIに投げない。APIキー無ければ何もしない。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return items
    from anthropic import Anthropic
    client = Anthropic()
    n = 0
    for it in items:
        if it.get("score", 0) < score_threshold:
            continue
        if n >= max_calls:
            print(f"[ai/{kind}] 上限到達、以降スキップ"); break
        try:
            it["ai"] = generate_group_comment(client, it, kind, backdrop); n += 1
            print(f"[ai/{kind}] {it.get('name')} OK")
        except Exception as e:
            it.setdefault("ai", ""); print(f"[ai/{kind}] {it.get('name')} 失敗: {e}")
    print(f"[ai/{kind}] 生成 {n} 件")
    return items


if __name__ == "__main__":
    pw = monthly_password("MASTER_SECRET_EXAMPLE", "2026-09")
    print("今月のパスワード例:", pw)
    import json
    blob = encrypt(pw, json.dumps({"cards": [{"name": "BTC", "ai": "テスト"}]}, ensure_ascii=False))
    print("暗号化blob(先頭):", blob[:48], "...")
