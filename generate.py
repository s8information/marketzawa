#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
marketzawa - 日次データ生成スクリプト

毎朝これを実行すると、全市場の実データ(過去1年)を取得して異常を検知し、
各対象に「直近トレンド・値位置・過去の類似局面のその後」＋市場全体の状況を材料として
AIの見立てを生成、有料部分を暗号化して index.html の埋め込み枠を書き換えます。

必要ライブラリ: pip install yfinance requests anthropic cryptography
環境変数(GitHub Secrets): ANTHROPIC_API_KEY, MARKETZAWA_MASTER_KEY
出力: index.html の埋め込み枠を上書き
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
# 1. 監視対象
# ============================================================
STOCKS = {
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
    "ビットコイン":   ("BTCUSDT", "暗号資産"),
    "イーサリアム":   ("ETHUSDT", "暗号資産"),
    "ソラナ":         ("SOLUSDT", "暗号資産"),
}
# 米国11セクター（SPDRセクターETF）
SECTORS = {
    "情報技術": "XLK", "通信サービス": "XLC", "エネルギー": "XLE", "素材": "XLB",
    "一般消費財": "XLY", "資本財": "XLI", "金融": "XLF", "公益": "XLU",
    "不動産": "XLRE", "ヘルスケア": "XLV", "生活必需品": "XLP",
}
# テーマ別（テーマ系ETF）※入れ替え自由
THEMES = {
    "半導体": "SOXX", "生成AI": "BOTZ", "防衛": "ITA", "宇宙": "UFO",
    "暗号資産関連株": "BITQ", "EV・電池": "LIT", "バイオ": "XBI", "クリーンエネ": "ICLN",
}
# 米国債金利（Yahoo利回り指数）
RATES = {
    "3ヶ月": "^IRX", "5年": "^FVX", "10年": "^TNX", "30年": "^TYX",
}


# ============================================================
# 2. データ取得（過去1年）
# ============================================================
def get_stock(ticker):
    df = yf.download(ticker, period="1y", interval="1d", progress=False)
    if df.empty:
        return None, None
    closes = [float(x) for x in df["Close"].values.flatten()]
    if "Volume" in df.columns:
        vols = [float(x) for x in df["Volume"].values.flatten()]
    else:
        vols = [0.0] * len(closes)
    return closes, vols

def get_crypto(symbol):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": "1d", "limit": 365}
    data = requests.get(url, params=params, timeout=20).json()
    closes = [float(d[4]) for d in data]
    vols   = [float(d[5]) for d in data]
    return closes, vols


# ============================================================
# 3. 分析ヘルパー（トレンド・値位置・過去の類似）
# ============================================================
def _rets(closes):
    return [(closes[i]-closes[i-1])/closes[i-1] for i in range(1, len(closes))]

def _rolling_z(rets, win=20):
    zs = [None]*len(rets)
    for t in range(win, len(rets)):
        w = rets[t-win:t]
        mu = statistics.mean(w); sd = statistics.pstdev(w) or 1e-9
        zs[t] = (rets[t]-mu)/sd
    return zs

def trend_desc(closes):
    if len(closes) < 6:
        return ""
    r5 = (closes[-1]-closes[-6])/closes[-6]*100
    streak = 0; sign = None
    for i in range(len(closes)-1, 0, -1):
        d = closes[i]-closes[i-1]
        s = 1 if d > 0 else (-1 if d < 0 else 0)
        if s == 0: break
        if sign is None: sign = s; streak = 1
        elif s == sign: streak += 1
        else: break
    txt = f"5日で{r5:+.1f}%"
    if streak >= 2 and sign:
        txt += f"、{streak}日{'続伸' if sign > 0 else '続落'}"
    return txt

def level_desc(closes, win=63):
    seg = closes[-win:] if len(closes) >= win else closes
    hi = max(seg); lo = min(seg); c = closes[-1]
    if c >= hi*0.999: return "3ヶ月高値を更新圏"
    if c <= lo*1.001: return "3ヶ月安値を更新圏"
    to_hi = (hi-c)/c*100; from_lo = (c-lo)/lo*100
    if to_hi <= 1.5:   return f"3ヶ月高値まで残り{to_hi:.1f}%"
    if from_lo <= 1.5: return f"3ヶ月安値まで残り{from_lo:.1f}%"
    return f"高値まで{to_hi:.1f}% / 安値まで{from_lo:.1f}%"

def past_similar(closes, z_today, band=0.6, min_move=1.5, horizons=(1, 5)):
    """今日と似たZスコアの過去局面を探し、その後の平均リターンを文にする（都度計算）。"""
    if z_today is None or abs(z_today) < min_move:
        return ""
    rets = _rets(closes)
    zs = _rolling_z(rets)
    sign = 1 if z_today > 0 else -1
    maxh = max(horizons)
    matches = []
    for t in range(len(zs)):
        z = zs[t]
        if z is None: continue
        if (1 if z > 0 else -1) != sign: continue
        if abs(z) < min_move: continue
        if abs(z - z_today) > band: continue
        ci = t + 1  # 該当日の終値index
        if ci + maxh >= len(closes): continue
        matches.append({h: (closes[ci+h]-closes[ci])/closes[ci]*100 for h in horizons})
    n = len(matches)
    if n < 3:
        return ""
    head = (f"過去1年で同程度（Zスコア{'+' if sign>0 else '-'}{abs(z_today):.1f}前後）の"
            f"{'急伸' if sign>0 else '急落'}は{n}回")
    parts = [head]
    for h in horizons:
        vals = [m[h] for m in matches]
        up = sum(1 for v in vals if v > 0)
        parts.append(f"{h}営業日後は平均{statistics.mean(vals):+.1f}%（上昇{up}/下落{n-up}）")
    return "。".join(parts) + "。"


def score_to_level(s100):
    if s100 >= 80: return "かなり荒れ", "#F87171"
    if s100 >= 65: return "ざわつき",   "#FB923C"
    if s100 >= 50: return "やや動意",   "#FBBF24"
    if s100 >= 40: return "おおむね平常", "#A3E635"
    if s100 >= 30: return "やや静か",   "#A3E635"
    return "静か", "#5EE7D0"


# ============================================================
# 4. 個別カードの判定
# ============================================================
def analyze(name, closes, vols, market):
    if not closes or len(closes) < 25:
        return None
    rets = _rets(closes)
    window = rets[-21:-1]
    mu = statistics.mean(window); sd = statistics.pstdev(window) or 1e-9
    z = (rets[-1]-mu)/sd
    today_ret = rets[-1]*100

    vol_ratio = None
    if vols and sum(vols[-21:-1]) > 0:
        vavg = statistics.mean(vols[-21:-1])
        vol_ratio = vols[-1]/vavg if vavg else None

    s_move = min(abs(z)/4, 1)
    s_vol  = min(max((vol_ratio or 1)-1, 0)/4, 1) if vol_ratio else 0
    score  = max(s_move, s_vol)
    is_anom = (abs(z) >= 2) or (vol_ratio is not None and vol_ratio >= 3)

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
    if badge is None:
        badge, cls = "注目", "b-calm"
        desc = f"前日比{today_ret:+.1f}%（Zスコア {z:+.1f}）。本日の中では相対的に動きがありました。"

    facts = [["変動", f"{today_ret:+.1f}%（Zスコア {z:+.1f}）"]]
    if vol_ratio is not None:
        facts.append(["出来高", f"平均の{vol_ratio:.1f}倍"])
    tr = trend_desc(closes);  facts.append(["直近", tr]) if tr else None
    lv = level_desc(closes);  facts.append(["水準", lv]) if lv else None

    return {
        "score": round(score, 3),
        "is_anom": is_anom,
        "card": {
            "badge": badge, "cls": cls, "mkt": market, "name": name,
            "chg": f"{'+' if today_ret>=0 else ''}{today_ret:.1f}%",
            "chgcls": "up" if today_ret >= 0 else "down",
            "desc": desc, "facts": facts,
            "hist": past_similar(closes, z),   # 過去の類似ケース（都度計算）
            "ai": "",
        },
    }


# ============================================================
# 5. セクター/テーマの判定
# ============================================================
def analyze_group(name, closes, vols):
    if not closes or len(closes) < 25:
        return None
    rets = _rets(closes)
    window = rets[-21:-1]
    mu = statistics.mean(window); sd = statistics.pstdev(window) or 1e-9
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
    tr = trend_desc(closes); facts.append(["直近", tr]) if tr else None

    return {"name": name, "score": score100, "lv": lv, "color": color,
            "facts": facts, "hist": past_similar(closes, z), "ai": ""}

def build_groups(mapping):
    out = []
    for name, ticker in mapping.items():
        try:
            c, v = get_stock(ticker)
            g = analyze_group(name, c, v)
            if g: out.append(g); print(f"OK  [group] {name}")
            else: print(f"--  [group] {name}: データ不足")
        except Exception as e:
            print(f"NG  [group] {name}: {e}")
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


# ============================================================
# 6. 米国債金利
# ============================================================
def _norm_yield(closes):
    # ^TNX 等は 42.5 のように10倍表記の場合がある。US利回りは<20%として自動補正
    if closes and statistics.median(closes[-20:]) > 20:
        return [x/10 for x in closes]
    return closes

def analyze_rate(name, ylds):
    if not ylds or len(ylds) < 25:
        return None
    dbp = [(ylds[i]-ylds[i-1])*100 for i in range(1, len(ylds))]  # 日次bp変化
    window = dbp[-21:-1]
    mu = statistics.mean(window); sd = statistics.pstdev(window) or 1e-9
    z = (dbp[-1]-mu)/sd
    score100 = round(min(abs(z)/4, 1) * 100)
    lv, color = score_to_level(score100)
    r5bp = (ylds[-1]-ylds[-6])*100 if len(ylds) >= 6 else 0
    facts = [
        ["利回り", f"{ylds[-1]:.2f}%"],
        ["前日比", f"{dbp[-1]:+.0f}bp（Zスコア {z:+.1f}）"],
        ["直近", f"5日で{r5bp:+.0f}bp"],
    ]
    return {"name": name, "score": score100, "lv": lv, "color": color,
            "yld": f"{ylds[-1]:.2f}", "chgbp": f"{dbp[-1]:+.0f}",
            "facts": facts, "hist": "", "ai": ""}

def build_rates():
    out, latest = [], {}
    for name, ticker in RATES.items():
        try:
            c, _ = get_stock(ticker)
            c = _norm_yield(c)
            r = analyze_rate(name, c)
            if r:
                out.append(r); latest[name] = c[-1]; print(f"OK  [rate] {name}")
            else:
                print(f"--  [rate] {name}: データ不足")
        except Exception as e:
            print(f"NG  [rate] {name}: {e}")
    # イールドカーブ（10年-3ヶ月）
    curve = ""
    if "10年" in latest and "3ヶ月" in latest:
        spread = (latest["10年"]-latest["3ヶ月"])*100
        curve = f"10年-3ヶ月 {spread:+.0f}bp（{'逆イールド' if spread < 0 else '順イールド'}）"
    out.sort(key=lambda x: x["score"], reverse=True)
    return out, curve


# ============================================================
# 7. 市場全体の状況（AIに渡す backdrop）
# ============================================================
def _pct_last(closes):
    if not closes or len(closes) < 2: return None
    return (closes[-1]-closes[-2])/closes[-2]*100

def build_backdrop(raw, rate_latest):
    """raw: {name: closes}。主要資産の当日変化を1行にまとめてAIへ。"""
    bits = []
    def add(label, name, unit="%"):
        p = _pct_last(raw.get(name))
        if p is not None: bits.append(f"{label} {p:+.1f}{unit}")
    add("S&P500", "S&P500"); add("ドル円", "ドル円")
    add("金", "ゴールド"); add("BTC", "ビットコイン"); add("原油", "原油 WTI")
    if "10年" in rate_latest:
        y = rate_latest["10年"]; bits.append(f"米10年 {y[-1]:.2f}%（{ (y[-1]-y[-2])*100:+.0f}bp）" if len(y) >= 2 else f"米10年 {y[-1]:.2f}%")
    return " / ".join(bits)


# ============================================================
# 8. ざわつき指数
# ============================================================
def zawatsuki_index(scores):
    return min(int(sum(scores) / 6 * 100), 100)


# ============================================================
# 9. メイン
# ============================================================
FREE_KEYS = ("badge", "cls", "mkt", "name", "chg", "chgcls", "desc")
GROUP_FREE = ("name", "score", "lv", "color")
RATE_FREE = ("name", "score", "lv", "color", "yld", "chgbp")


def main():
    results, scores, raw = [], [], {}

    for name, (ticker, market) in STOCKS.items():
        try:
            c, v = get_stock(ticker); raw[name] = c
            r = analyze(name, c, v, market)
            if r:
                scores.append(r["score"]); results.append((r["score"], r["is_anom"], r["card"]))
            print(f"OK  {name}")
        except Exception as e:
            print(f"NG  {name}: {e}")

    for name, (symbol, market) in CRYPTOS.items():
        try:
            c, v = get_crypto(symbol); raw[name] = c
            r = analyze(name, c, v, market)
            if r:
                scores.append(r["score"]); results.append((r["score"], r["is_anom"], r["card"]))
            print(f"OK  {name}")
        except Exception as e:
            print(f"NG  {name}: {e}")

    # 金利（backdrop用に生利回りも保持）
    rate_latest = {}
    for name, ticker in RATES.items():
        try:
            c, _ = get_stock(ticker); rate_latest[name] = _norm_yield(c)
        except Exception as e:
            print(f"NG  [rate raw] {name}: {e}")

    backdrop = build_backdrop(raw, rate_latest)
    print(f"   backdrop: {backdrop}")

    results.sort(key=lambda x: x[0], reverse=True)
    cards = [c for s, a, c in results]        # 全カード表示（無料3＋有料それ以降）

    sectors = build_groups(SECTORS)
    themes  = build_groups(THEMES)
    rates, curve = build_rates()

    # --- AIの見立て（材料＝facts＋hist＋backdrop）---
    cards   = fill_ai(cards, backdrop=backdrop)
    sectors = fill_ai_groups(sectors, "セクター",   backdrop=backdrop)
    themes  = fill_ai_groups(themes,  "テーマ",     backdrop=backdrop)
    rates   = fill_ai_groups(rates,   "米国債金利", backdrop=backdrop, score_threshold=40)

    # --- 無料（見出し/スコアのみ）と 有料（全情報を暗号化）に分割 ---
    free = [{k: c.get(k) for k in FREE_KEYS} for c in cards[:3]]
    sectors_free = [{k: g[k] for k in GROUP_FREE} for g in sectors]
    themes_free  = [{k: g[k] for k in GROUP_FREE} for g in themes]
    rates_free   = [{k: g[k] for k in RATE_FREE} for g in rates]

    enc = None
    master = os.environ.get("MARKETZAWA_MASTER_KEY")
    if master:
        pw = monthly_password(master)
        payload = {"cards": cards, "sectors": sectors, "themes": themes, "rates": rates}
        enc = encrypt(pw, json.dumps(payload, ensure_ascii=False))
        print(f"   暗号化: OK（対象月 {current_ym()}）")
    else:
        print("!! MARKETZAWA_MASTER_KEY 未設定：暗号化スキップ（有料解除は無効）")

    out = {
        "updated": datetime.datetime.now().strftime("%Y/%m/%d %H:%M"),
        "index": zawatsuki_index(scores),
        "free": free,
        "paid_count": max(0, len(cards) - 3),
        "sectors": sectors_free,
        "themes": themes_free,
        "rates": rates_free,
        "curve": curve,
        "enc": enc,
    }

    embed = "var EMBEDDED_DATA = " + json.dumps(out, ensure_ascii=False) + ";"
    with open("index.html", "r", encoding="utf-8") as f:
        html = f.read()
    import re
    pattern = re.compile(
        r"(/\* MARKETZAWA_DATA_START[^\n]*\*/\n).*?(\n\s*/\* MARKETZAWA_DATA_END \*/)",
        re.DOTALL)
    if not pattern.search(html):
        print("!! index.html に埋め込み枠が見つかりません。中止。")
        return
    html = pattern.sub(lambda m: m.group(1) + "  " + embed + m.group(2), html)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("\n→ index.html 更新完了")
    print(f"   ざわつき指数: {out['index']}")
    print(f"   カード {len(cards)}（無料{len(free)}/有料{out['paid_count']}） / "
          f"セクター{len(sectors)} / テーマ{len(themes)} / 金利{len(rates)}")
    print(f"   {curve}")


if __name__ == "__main__":
    main()
