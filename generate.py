#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
marketzawa - 日次データ生成スクリプト

毎朝これを実行すると、全市場の実データを取得して異常を検知し、
各カードにAIの見立てを付け、有料部分を暗号化して index.html の
埋め込み枠（MARKETZAWA_DATA）を書き換えます。

必要ライブラリ:
    pip install yfinance requests anthropic cryptography

環境変数（GitHub Secrets）:
    ANTHROPIC_API_KEY      … AIの見立て生成用（無ければ ai 欄は空のまま動く）
    MARKETZAWA_MASTER_KEY  … 月替わりパスワードの種（無ければ暗号化スキップ＝解除無効）

実行:
    python generate.py
出力:
    index.html の埋め込み枠を上書き
"""

import json, statistics, datetime, sys, os

try:
    import yfinance as yf
    import requests
except ImportError:
    print("先に `pip install yfinance requests anthropic cryptography` を実行してください")
    sys.exit(1)

from marketzawa_ai import fill_ai, monthly_password, encrypt, current_ym


# ============================================================
# 1. 監視する対象（yfinanceのティッカー）
#    暗号資産だけ Binance を使う（yfinanceより素直なため）
# ============================================================
STOCKS = {
    # 表示名: (ティッカー, 市場ラベル)
    "エヌビディア":   ("NVDA",   "米国株"),
    "テスラ":         ("TSLA",   "米国株"),
    "S&P500":        ("SPY",    "株価指数"),
    "半導体指数 SOX": ("SOXX",   "米国株"),
    "ゴールド":       ("GLD",    "金"),
    "原油 WTI":       ("USO",    "商品"),
    "ドル円":         ("JPY=X",  "為替"),
    "ユーロ円":       ("EURJPY=X","為替"),
}
CRYPTOS = {
    # 表示名: (Binanceシンボル, 市場ラベル)
    "ビットコイン":   ("BTCUSDT", "暗号資産"),
    "イーサリアム":   ("ETHUSDT", "暗号資産"),
    "ソラナ":         ("SOLUSDT", "暗号資産"),
}


# ============================================================
# 2. データ取得
# ============================================================
def get_stock(ticker):
    """yfinanceで日次の終値・出来高リストを返す"""
    df = yf.download(ticker, period="3mo", interval="1d", progress=False)
    if df.empty:
        return None, None
    closes  = [float(x) for x in df["Close"].values.flatten()]
    # 為替は出来高が無いことがあるので0埋め
    if "Volume" in df.columns:
        vols = [float(x) for x in df["Volume"].values.flatten()]
    else:
        vols = [0.0] * len(closes)
    return closes, vols

def get_crypto(symbol):
    """Binanceで日次の終値・出来高リストを返す"""
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": "1d", "limit": 90}
    data = requests.get(url, params=params, timeout=20).json()
    closes = [float(d[4]) for d in data]
    vols   = [float(d[5]) for d in data]
    return closes, vols


# ============================================================
# 3. 異常判定（値動きZスコア / 出来高比）
# ============================================================
def analyze(name, closes, vols, market):
    if not closes or len(closes) < 25:
        return None
    rets = [(closes[i]-closes[i-1])/closes[i-1] for i in range(1, len(closes))]
    window = rets[-21:-1]
    mu = statistics.mean(window)
    sd = statistics.pstdev(window) or 1e-9
    z = (rets[-1]-mu)/sd
    today_ret = rets[-1]*100

    # 出来高比（出来高データがある場合のみ）
    vol_ratio = None
    if vols and sum(vols[-21:-1]) > 0:
        vavg = statistics.mean(vols[-21:-1])
        vol_ratio = vols[-1]/vavg if vavg else None

    # 各シグナルを0-1に正規化
    s_move = min(abs(z)/4, 1)
    s_vol  = min(max((vol_ratio or 1)-1, 0)/4, 1) if vol_ratio else 0
    score  = max(s_move, s_vol)

    # 異常かどうか
    is_anom = (abs(z) >= 2) or (vol_ratio is not None and vol_ratio >= 3)

    # バッジと説明（無料側の一言）
    badge = cls = desc = None
    if vol_ratio is not None and vol_ratio >= 3 and s_vol >= s_move:
        badge, cls = "出来高異常", "b-vol"
        desc = f"出来高が平均の{vol_ratio:.1f}倍。普段より取引が急増しています。"
    elif today_ret <= 0 and abs(z) >= 2:
        badge, cls = "急落", "b-down"
        desc = f"前日比{today_ret:.1f}%、変動{abs(z):.1f}σ。普段より大きく下げています。"
    elif today_ret > 0 and abs(z) >= 2:
        badge, cls = "急騰", "b-up"
        desc = f"前日比+{today_ret:.1f}%、変動{abs(z):.1f}σ。普段より強く買われています。"

    # 非異常時のデフォルト（相対トップ埋め用の中立バッジ）
    if badge is None:
        badge, cls = "注目", "b-calm"
        desc = f"前日比{today_ret:+.1f}%（Zスコア {z:+.1f}）。本日の中では相対的に動きがありました。"

    facts = [["変動", f"{today_ret:+.1f}%（Zスコア {z:+.1f}）"]]
    if vol_ratio is not None:
        facts.append(["出来高", f"平均の{vol_ratio:.1f}倍"])

    # card は常に生成する（異常が少ない日は上位を相対トップで埋めるため）
    return {
        "score": round(score, 3),
        "is_anom": is_anom,
        "card": {
            "badge": badge, "cls": cls, "mkt": market, "name": name,
            "chg": f"{'+' if today_ret>=0 else ''}{today_ret:.1f}%",
            "chgcls": "up" if today_ret >= 0 else "down",
            "desc": desc,
            "facts": facts,
            "hist": "",   # 過去の類似ケース（履歴が貯まったら埋める。暗号化対象）
            "ai": "",     # AIの見立て（fill_ai が埋める。暗号化対象）
        },
    }


# ============================================================
# 4. ざわつき指数（当日の異常強度を0-100へ。まずは簡易版）
# ============================================================
def zawatsuki_index(scores):
    R = sum(scores)
    idx = min(int(R / 6 * 100), 100)
    return idx


# ============================================================
# 5. メイン
# ============================================================
# 無料（平文）で出してよいキー＝見出しだけ。facts/hist/ai は有料なので含めない
FREE_KEYS = ("badge", "cls", "mkt", "name", "chg", "chgcls", "desc")


def main():
    results = []
    scores = []

    for name, (ticker, market) in STOCKS.items():
        try:
            c, v = get_stock(ticker)
            r = analyze(name, c, v, market)
            if r:
                scores.append(r["score"])
                results.append((r["score"], r["is_anom"], r["card"]))
            print(f"OK  {name}")
        except Exception as e:
            print(f"NG  {name}: {e}")

    for name, (symbol, market) in CRYPTOS.items():
        try:
            c, v = get_crypto(symbol)
            r = analyze(name, c, v, market)
            if r:
                scores.append(r["score"])
                results.append((r["score"], r["is_anom"], r["card"]))
            print(f"OK  {name}")
        except Exception as e:
            print(f"NG  {name}: {e}")

    # スコアの強い順に並べる（異常は score>=0.5 になるので自然に上位へ）
    results.sort(key=lambda x: x[0], reverse=True)
    n_anom = sum(1 for s, a, c in results if a)
    # 異常は全部出す。3件未満の日は、相対的に最も動いた上位で最低3件まで埋める
    take = max(3, n_anom)
    cards = [c for s, a, c in results[:take]]

    # --- AIの見立てを各カードに付与（全カード＝「全カードのAI分析」の約束どおり）---
    cards = fill_ai(cards)

    # --- 無料（見出しのみ）と 有料（全情報を暗号化）に分割 ---
    free = [{k: c.get(k) for k in FREE_KEYS} for c in cards[:3]]

    enc = None
    master = os.environ.get("MARKETZAWA_MASTER_KEY")
    if master:
        pw = monthly_password(master)
        enc = encrypt(pw, json.dumps({"cards": cards}, ensure_ascii=False))
        print(f"   暗号化: OK（対象月 {current_ym()}）")
    else:
        print("!! MARKETZAWA_MASTER_KEY 未設定：暗号化をスキップ（有料解除は無効になります）")

    out = {
        "updated": datetime.datetime.now().strftime("%Y/%m/%d %H:%M"),
        "index": zawatsuki_index(scores),
        "free": free,
        "paid_count": max(0, len(cards) - 3),
        "enc": enc,
    }

    # index.html の埋め込み枠を書き換える（外部ファイルを使わない安定方式）
    embed = "var EMBEDDED_DATA = " + json.dumps(out, ensure_ascii=False) + ";"
    with open("index.html", "r", encoding="utf-8") as f:
        html = f.read()

    import re
    pattern = re.compile(
        r"(/\* MARKETZAWA_DATA_START[^\n]*\*/\n).*?(\n\s*/\* MARKETZAWA_DATA_END \*/)",
        re.DOTALL,
    )
    if not pattern.search(html):
        print("!! index.html に埋め込み枠が見つかりません。処理を中止します。")
        return
    html = pattern.sub(lambda m: m.group(1) + "  " + embed + m.group(2), html)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n→ index.html 更新完了")
    print(f"   ざわつき指数: {out['index']}")
    print(f"   カード数: {len(cards)}（無料 {len(free)} / 有料 {out['paid_count']}）")


if __name__ == "__main__":
    main()
