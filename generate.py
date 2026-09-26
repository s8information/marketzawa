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

from marketzawa_ai import fill_ai, fill_ai_groups, monthly_password, encrypt, current_ym


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

# セクター別ざわつき（米国11セクター＝SPDRセクターETF）表示名: ティッカー
SECTORS = {
    "情報技術":     "XLK",
    "通信サービス": "XLC",
    "エネルギー":   "XLE",
    "素材":         "XLB",
    "一般消費財":   "XLY",
    "資本財":       "XLI",
    "金融":         "XLF",
    "公益":         "XLU",
    "不動産":       "XLRE",
    "ヘルスケア":   "XLV",
    "生活必需品":   "XLP",
}

# テーマ別ざわつき（テーマ系ETF）表示名: ティッカー ※入れ替え自由
THEMES = {
    "半導体":         "SOXX",
    "生成AI":         "BOTZ",
    "防衛":           "ITA",
    "宇宙":           "UFO",
    "暗号資産関連株": "BITQ",
    "EV・電池":       "LIT",
    "バイオ":         "XBI",
    "クリーンエネ":   "ICLN",
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
def score_to_level(s100):
    """0-100 のざわつきスコアを (ラベル, 色) に変換。index.htmlの色と揃える。"""
    if s100 >= 80: return "かなり荒れ", "#F87171"
    if s100 >= 65: return "ざわつき",   "#FB923C"
    if s100 >= 50: return "やや動意",   "#FBBF24"
    if s100 >= 40: return "おおむね平常", "#A3E635"
    if s100 >= 30: return "やや静か",   "#A3E635"
    return "静か", "#5EE7D0"


def analyze_group(name, closes, vols):
    """セクター/テーマ1件を {name, score(0-100), lv, color, facts, ai} で返す。"""
    if not closes or len(closes) < 25:
        return None
    rets = [(closes[i]-closes[i-1])/closes[i-1] for i in range(1, len(closes))]
    window = rets[-21:-1]
    mu = statistics.mean(window)
    sd = statistics.pstdev(window) or 1e-9
    z = (rets[-1]-mu)/sd
    today_ret = rets[-1]*100

    vol_ratio = None
    if vols and sum(vols[-21:-1]) > 0:
        vavg = statistics.mean(vols[-21:-1])
        vol_ratio = vols[-1]/vavg if vavg else None

    s_move = min(abs(z)/4, 1)
    s_vol  = min(max((vol_ratio or 1)-1, 0)/4, 1) if vol_ratio else 0
    score100 = round(max(s_move, s_vol) * 100)
    lv, color = score_to_level(score100)

    facts = [["変動", f"{today_ret:+.1f}%（Zスコア {z:+.1f}）"]]
    if vol_ratio is not None:
        facts.append(["出来高", f"平均の{vol_ratio:.1f}倍"])

    return {"name": name, "score": score100, "lv": lv, "color": color, "facts": facts, "ai": ""}


def build_groups(mapping):
    """SECTORS/THEMES から実データを取ってグループのリストを作る（スコア降順）。"""
    out = []
    for name, ticker in mapping.items():
        try:
            c, v = get_stock(ticker)
            g = analyze_group(name, c, v)
            if g:
                out.append(g)
                print(f"OK  [group] {name}")
            else:
                print(f"--  [group] {name}: データ不足")
        except Exception as e:
            print(f"NG  [group] {name}: {e}")
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


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
    # 表示カードは全件（無料3＋有料それ以降）。全カードにAI分析を付ける
    take = len(results)
    cards = [c for s, a, c in results[:take]]

    # --- AIの見立てを各カードに付与 ---
    cards = fill_ai(cards)

    # --- セクター/テーマ：実データ取得 → AI見立て（ざわついた群のみ）---
    sectors = build_groups(SECTORS)
    themes  = build_groups(THEMES)
    sectors = fill_ai_groups(sectors, "セクター")   # score>=50 の群だけAIに投げる
    themes  = fill_ai_groups(themes,  "テーマ")

    # --- 無料（見出し/スコアのみ）と 有料（全情報を暗号化）に分割 ---
    free = [{k: c.get(k) for k in FREE_KEYS} for c in cards[:3]]
    GROUP_FREE = ("name", "score", "lv", "color")   # 無料はグリッド表示に必要な分だけ
    sectors_free = [{k: g[k] for k in GROUP_FREE} for g in sectors]
    themes_free  = [{k: g[k] for k in GROUP_FREE} for g in themes]

    enc = None
    master = os.environ.get("MARKETZAWA_MASTER_KEY")
    if master:
        pw = monthly_password(master)
        payload = {"cards": cards, "sectors": sectors, "themes": themes}
        enc = encrypt(pw, json.dumps(payload, ensure_ascii=False))
        print(f"   暗号化: OK（対象月 {current_ym()}）")
    else:
        print("!! MARKETZAWA_MASTER_KEY 未設定：暗号化をスキップ（有料解除は無効になります）")

    out = {
        "updated": datetime.datetime.now().strftime("%Y/%m/%d %H:%M"),
        "index": zawatsuki_index(scores),
        "free": free,
        "paid_count": max(0, len(cards) - 3),
        "sectors": sectors_free,
        "themes": themes_free,
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
    print(f"   セクター {len(sectors)} 件 / テーマ {len(themes)} 件")


if __name__ == "__main__":
    main()
