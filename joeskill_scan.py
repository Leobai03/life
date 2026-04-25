import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

UA = {"User-Agent": "Mozilla/5.0 (joeskill scanner)"}
STABLE = {"USDC", "FDUSD", "TUSD", "DAI", "EUR", "BUSD", "USDP", "AEUR", "WBTC"}
LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
MAJORS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT"}


def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"), strict=False)


def safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default


def fmt_usd(n):
    if n is None:
        return "n/a"
    if abs(n) >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{n:.0f}"


def pct(x):
    return "n/a" if x is None else f"{x:.2f}%"


def get_daily(symbol, limit=15):
    q = urllib.parse.urlencode({"symbol": symbol, "interval": "1d", "limit": str(limit)})
    return fetch_json(f"https://data-api.binance.vision/api/v3/klines?{q}", timeout=20)


def candle_stats(kl):
    out = []
    for k in kl:
        o, h, l, c = map(float, [k[1], k[2], k[3], k[4]])
        qv = float(k[7])
        gain = (c / o - 1) * 100 if o else 0
        out.append({"open": o, "high": h, "low": l, "close": c, "qv": qv, "gain": gain, "green": c > o})
    return out


def consecutive_green(candles):
    n = 0
    for c in reversed(candles):
        if c["green"]:
            n += 1
        else:
            break
    return n


def max_rolling_gain(candles, window):
    best = -999
    for i in range(0, len(candles) - window + 1):
        start = candles[i]["open"]
        end = candles[i + window - 1]["close"]
        if start:
            best = max(best, (end / start - 1) * 100)
    return best


def volume_spike_bad(candles):
    if len(candles) < 8:
        return False, None
    latest = candles[-1]["qv"]
    avg = sum(c["qv"] for c in candles[-7:-1]) / 6
    ratio = latest / avg if avg else None
    return (ratio is not None and ratio >= 2.0), ratio


def main():
    status = []
    spot_url = "https://data-api.binance.vision/api/v3/ticker/24hr"
    prod_url = "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products?includeEtf=true"
    spot = fetch_json(spot_url)
    status.append(f"Binance Vision 24h ticker OK: {len(spot)} rows")
    try:
        prod = fetch_json(prod_url)
        products = prod.get("data", []) if isinstance(prod, dict) else []
        status.append(f"Binance product metadata OK: {len(products)} rows")
    except Exception as e:
        products = []
        status.append(f"Binance product metadata failed: {type(e).__name__}: {e}")
    prod_by_symbol = {p.get("s"): p for p in products if isinstance(p, dict)}

    rows = []
    for t in spot:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym.endswith(LEVERAGED_SUFFIXES):
            continue
        base = sym[:-4]
        if base in STABLE or sym in MAJORS:
            continue
        price = safe_float(t.get("lastPrice"))
        qv = safe_float(t.get("quoteVolume"), 0)
        chg = safe_float(t.get("priceChangePercent"), 0)
        high = safe_float(t.get("highPrice"))
        low = safe_float(t.get("lowPrice"))
        if not price or not qv:
            continue
        p = prod_by_symbol.get(sym, {})
        cs = p.get("cs")
        mc = float(cs) * price if isinstance(cs, (int, float)) and cs else None
        rows.append({"symbol": sym, "base": base, "name": p.get("an") or base, "price": price, "qv": qv, "chg": chg, "high": high, "low": low, "mc": mc, "tags": p.get("tags") or []})

    # Pull daily history for likely relevant universe: high volume plus low-volume possible J3.
    candidates_universe = sorted(rows, key=lambda r: r["qv"], reverse=True)[:180]
    candidates_universe += [r for r in rows if 300_000 <= r["qv"] < 10_000_000][:120]
    seen = set()
    uniq = []
    for r in candidates_universe:
        if r["symbol"] not in seen:
            seen.add(r["symbol"])
            uniq.append(r)

    j2 = []
    j3 = []
    j4 = []
    no_chase = []
    checked = 0
    errors = []

    for r in uniq:
        sym = r["symbol"]
        try:
            kl = get_daily(sym, 15)
            time.sleep(0.035)
        except Exception as e:
            errors.append(f"{sym}:{type(e).__name__}")
            continue
        cs = candle_stats(kl)
        if len(cs) < 11:
            continue
        checked += 1
        prev_qv = cs[-2]["qv"]
        jump = r["qv"] / prev_qv if prev_qv else None
        bad_spike, vol_ratio = volume_spike_bad(cs)
        green_n = consecutive_green(cs)
        gains = [c["gain"] for c in cs[-4:]]
        gain3 = (cs[-1]["close"] / cs[-3]["open"] - 1) * 100 if len(cs) >= 3 else None
        gain4 = (cs[-1]["close"] / cs[-4]["open"] - 1) * 100 if len(cs) >= 4 else None
        max1 = max(c["gain"] for c in cs[-10:])
        max3 = max_rolling_gain(cs[-10:], 3)
        high10 = max(c["high"] for c in cs[-10:])
        pullback = (r["price"] / high10 - 1) * 100 if high10 else None
        avg3v = sum(c["qv"] for c in cs[-3:]) / 3
        prev7v = sum(c["qv"] for c in cs[-10:-3]) / 7
        vol_ok = avg3v >= prev7v * 0.85 if prev7v else True
        breakout = (r["high"] or r["price"]) * 1.005
        fail = (r["low"] or r["price"]) * 0.995

        if prev_qv < 20_000_000 and r["qv"] > 100_000_000 and jump:
            score = min(jump, 100) + max(0, min(r["chg"], 50))
            item = dict(r, strategy="J2 成交额突增", prev_qv=prev_qv, jump=jump, breakout=breakout, fail=fail, score=score, vol_ratio=vol_ratio)
            if r["chg"] > 35 or bad_spike:
                no_chase.append({**item, "reason": "成交额突增但可能已拉高/放量过猛"})
            else:
                j2.append(item)

        if (max1 >= 25 or max3 >= 45) and 300_000 <= r["qv"] < 10_000_000 and pullback is not None and -65 <= pullback <= -20 and r["chg"] >= -12:
            last3_big_red = all((not c["green"] and c["gain"] < -8) for c in cs[-3:])
            if not last3_big_red:
                score = (65 - abs(abs(pullback) - 42)) + min(max1, 80) / 4 + (r["qv"] / 1_000_000)
                j3.append(dict(r, strategy="J3 暴涨后缩量潜伏", max1=max1, max3=max3, high10=high10, pullback=pullback, breakout=breakout, fail=fail, score=score, vol_ratio=vol_ratio))

        seq = gains[-green_n:] if green_n in (3, 4) else []
        cum = gain3 if green_n == 3 else gain4 if green_n == 4 else None
        if green_n in (3, 4) and cum is not None and 12 <= cum <= 55 and r["qv"] >= 50_000_000 and vol_ok:
            if all(2 <= g <= 18 for g in seq) and not bad_spike:
                score = cum + math.log10(r["qv"] + 1) * 5
                j4.append(dict(r, strategy="J4A 日线匀速连涨", green_n=green_n, cum=cum, gains=seq, breakout=breakout, fail=fail, score=score, vol_ratio=vol_ratio))
            elif r["chg"] > 25 or bad_spike:
                no_chase.append(dict(r, strategy="J4A 近似但过热", green_n=green_n, cum=cum, gains=seq, breakout=breakout, fail=fail, score=cum or 0, vol_ratio=vol_ratio, reason="最新量能/涨幅偏过热"))

    def top(xs, n=5):
        return sorted(xs, key=lambda x: x.get("score", 0), reverse=True)[:n]

    report = []
    report.append(f"JoeSkill scan {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append("数据源状态：")
    for s in status:
        report.append(f"- {s}")
    report.append(f"- Filtered spot USDT universe: {len(rows)} symbols; daily klines checked: {checked}; errors: {len(errors)}")
    if errors[:5]:
        report.append(f"- Sample kline errors: {', '.join(errors[:5])}")

    sections = [("J2 成交额突增", top(j2)), ("J3 暴涨后缩量潜伏", top(j3)), ("J4A 日线匀速连涨", top(j4))]
    report.append("\n今日 Joe 主榜：")
    all_watch = []
    idx = 1
    for title, items in sections:
        report.append(f"\n{title}：")
        if not items:
            report.append("- 今天没有硬条件合格标的。")
            continue
        for it in items:
            all_watch.append(it)
            trig = ""
            if it["strategy"].startswith("J2"):
                trig = f"昨日日成交额 {fmt_usd(it['prev_qv'])} -> 当前24h {fmt_usd(it['qv'])}，放大 {it['jump']:.1f}x"
            elif it["strategy"].startswith("J3"):
                trig = f"10日最大单日 {pct(it['max1'])} / 3日 {pct(it['max3'])}，距10日高点 {pct(it['pullback'])}，当前24h成交额 {fmt_usd(it['qv'])}"
            else:
                trig = f"连续{it['green_n']}根日线绿，累计 {pct(it['cum'])}，序列 {'/'.join(f'{g:.1f}%' for g in it['gains'])}，24h成交额 {fmt_usd(it['qv'])}"
            risk = "A" if it.get("score", 0) >= 80 else "B" if it.get("score", 0) >= 50 else "C"
            report.append(f"{idx}. {it['symbol']} ({it['name']}) — {it['strategy']} — 优先级 {risk}")
            report.append(f"   触发：{trig}")
            vr = it.get("vol_ratio")
            vr_text = f"{vr:.2f}x" if vr is not None else "n/a"
            report.append(f"   数字：价格 {it['price']:.8g} / 24h {pct(it['chg'])} / 量能比 {vr_text}")
            report.append(f"   盯盘：突破 {it['breakout']:.8g} / 失效 {it['fail']:.8g}")
            idx += 1

    watch3 = top(all_watch, 3)
    report.append("\n今日只值得盯的 3 个：")
    if watch3:
        for it in watch3:
            report.append(f"- {it['symbol']}：{it['strategy']}，24h {pct(it['chg'])}，成交额 {fmt_usd(it['qv'])}")
    else:
        report.append("- 暂无硬条件足够强的标的。")

    report.append("\n今日不要追的 3 个：")
    for it in top(no_chase, 3):
        report.append(f"- {it['symbol']}：{it.get('reason','过热/放量异常')}，24h {pct(it['chg'])}，成交额 {fmt_usd(it['qv'])}")
    if not no_chase:
        report.append("- 暂无明显过热排除项。")

    report.append("\n下一次扫描建议：J2 16:00/00:00 复扫；J3/J4A 明早 08:00 复扫。")
    report.append("提示：这是结构筛选，不是投资建议；任何标的都需要人工看图确认右侧量价。")
    print("\n".join(report))


if __name__ == "__main__":
    main()
