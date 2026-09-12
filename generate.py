#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
marketzawa - 日次データ生成スクリプト

毎朝これを実行すると、全市場の実データを取得して異常を検知し、
サイトが読み込む data.json を書き出します。

必要ライブラリ:
    pip install yfinance requests

実行:
    python generate.py
出力:
    data.json （HTMLと同じフォルダに置く）

AIの見立て（ai欄）は今は空です。後から追加予定。
"""

import json, statistics, datetime, sys

try:
    import yfinance as yf
    import requests
except ImportError:
    print("先に `pip install yfinance requests` を実行してください")
    sys.exit(1)


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

    facts = [["変動", f"{today_ret:+.1f}%（Zスコア {z:+.1f}）"]]
    if vol_ratio is not None:
        facts.append(["出来高", f"平均の{vol_ratio:.1f}倍"])

    return {
        "score": round(score, 3),
        "is_anom": is_anom,
        "card": {
            "badge": badge, "cls": cls, "mkt": market, "name": name,
            "chg": f"{'+' if today_ret>=0 else ''}{today_ret:.1f}%",
            "chgcls": "up" if today_ret >= 0 else "down",
            "desc": desc,
            "facts": facts,
            "hist": "",   # 後から: 過去の類似ケース
            "ai": "",     # 後から: AIの見立て
        } if is_anom else None,
    }


# ============================================================
# 4. ざわつき指数（当日の異常強度を0-100へ。まずは簡易版）
#    ※本来は過去分布と比較。運用しながら履歴を貯めて精緻化する。
# ============================================================
def zawatsuki_index(scores):
    # 全対象スコアの合計を、経験的な基準でスケーリング
    R = sum(scores)
    # 対象10数個で、平常時のRは1〜2程度、大荒れで5前後を想定した暫定式
    idx = min(int(R / 6 * 100), 100)
    return idx


# ============================================================
# 5. メイン
# ============================================================
def main():
    results = []
    scores = []

    for name, (ticker, market) in STOCKS.items():
        try:
            c, v = get_stock(ticker)
            r = analyze(name, c, v, market)
            if r:
                scores.append(r["score"])
                if r["card"]:
                    results.append((r["score"], r["card"]))
            print(f"OK  {name}")
        except Exception as e:
            print(f"NG  {name}: {e}")

    for name, (symbol, market) in CRYPTOS.items():
        try:
            c, v = get_crypto(symbol)
            r = analyze(name, c, v, market)
            if r:
                scores.append(r["score"])
                if r["card"]:
                    results.append((r["score"], r["card"]))
            print(f"OK  {name}")
        except Exception as e:
            print(f"NG  {name}: {e}")

    # 異常の強い順に並べる
    results.sort(key=lambda x: x[0], reverse=True)
    cards = [c for _, c in results]

    out = {
        "updated": datetime.datetime.now().strftime("%Y/%m/%d %H:%M"),
        "index": zawatsuki_index(scores),
        "cards": cards,
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
    print(f"   異常カード数: {len(cards)}")


if __name__ == "__main__":
    main()
