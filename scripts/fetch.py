#!/usr/bin/env python3
"""
The Trade — Server-side data fetcher v19.3
Critical fix: validates against NaN before writing JSON (NaN breaks JS parser).
"""

import json
import math
import time
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
import xml.etree.ElementTree as ET

import yfinance as yf

IST = timezone(timedelta(hours=5, minutes=30))
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}


def safe_float(x):
    """Return None if x is NaN/Inf/invalid, otherwise float."""
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


YAHOO_SYMBOLS = {
    "NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "INDIAVIX": "^INDIAVIX",
    "SP500": "^GSPC", "NASDAQ": "^IXIC", "DOW": "^DJI",
    "DAX": "^GDAXI", "FTSE": "^FTSE", "CAC": "^FCHI",
    "NIKKEI": "^N225", "HSI": "^HSI", "KOSPI": "^KS11",
    "DXY": "DX-Y.NYB",
    "USDINR": "INR=X", "EURUSD": "EURUSD=X",
    "BRENT": "BZ=F", "WTI": "CL=F",
    "GOLD": "GC=F", "SILVER": "SI=F", "COPPER": "HG=F",
    "BTC": "BTC-USD", "ETH": "ETH-USD",
}


def fetch_yf(symbol, key):
    """Fetch from yfinance. Skip rows with NaN. Use last 2 valid rows."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="15d", auto_adjust=False)
        if hist is None or len(hist) < 2:
            return None
        # Drop rows with NaN close
        hist = hist.dropna(subset=["Close"])
        if len(hist) < 2:
            return None
        latest = hist.iloc[-1]
        prev = hist.iloc[-2]
        price = safe_float(latest["Close"])
        prev_close = safe_float(prev["Close"])
        if price is None or prev_close is None or price <= 0 or prev_close <= 0:
            return None
        prev_high = safe_float(prev["High"]) or prev_close
        prev_low = safe_float(prev["Low"]) or prev_close
        prev_open = safe_float(prev["Open"]) or prev_close
        return {
            "price": round(price, 4),
            "prev": round(prev_close, 4),
            "change": round(price - prev_close, 4),
            "pct": round((price - prev_close) / prev_close * 100, 3),
            "prevHigh": round(prev_high, 4),
            "prevLow": round(prev_low, 4),
            "prevOpen": round(prev_open, 4),
            "ts": int(time.time()),
            "source": "yfinance",
        }
    except Exception as e:
        print(f"  {key} yf error: {e}")
        return None


def fetch_all_yf():
    out = {}
    for key, sym in YAHOO_SYMBOLS.items():
        v = fetch_yf(sym, key)
        if v:
            out[key] = v
            dec = 0 if key == "BTC" else 2
            print(f"  ✓ {key:10s} {v['price']:>12,.{dec}f}  ({v['pct']:+.2f}%)")
        else:
            print(f"  ✗ {key:10s} skipped (no valid data — likely market closed)")
        time.sleep(0.4)
    return out


def fetch_frankfurter_fx():
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=USD&to=INR",
                         headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return None
        j = r.json()
        rate = safe_float(j.get("rates", {}).get("INR"))
        if rate:
            return {
                "price": round(rate, 4), "prev": round(rate, 4),
                "change": 0, "pct": 0,
                "source": "frankfurter", "ts": int(time.time()),
            }
    except Exception:
        pass
    return None


def fetch_coingecko():
    out = {}
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true",
            headers=HEADERS, timeout=10)
        if r.status_code == 200:
            j = r.json()
            for ck, ok in [("bitcoin", "BTC"), ("ethereum", "ETH")]:
                c = j.get(ck, {})
                price = safe_float(c.get("usd"))
                pct = safe_float(c.get("usd_24h_change")) or 0
                if price:
                    prev = price / (1 + pct/100) if pct else price
                    out[ok] = {
                        "price": round(price, 0 if ok == "BTC" else 2),
                        "prev": round(prev, 0 if ok == "BTC" else 2),
                        "change": round(price - prev, 0 if ok == "BTC" else 2),
                        "pct": round(pct, 3),
                        "source": "coingecko", "ts": int(time.time()),
                    }
    except Exception as e:
        print(f"  CoinGecko error: {e}")
    return out


def fetch_nse_option_chain(symbol, max_retries=3):
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA, "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.nseindia.com/option-chain",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
    })
    for attempt in range(max_retries):
        try:
            session.get("https://www.nseindia.com/", timeout=10)
            time.sleep(1.5)
            session.get("https://www.nseindia.com/option-chain", timeout=10)
            time.sleep(1.5)
            url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
            r = session.get(url, timeout=15)
            if r.status_code == 200:
                try:
                    return r.json()
                except json.JSONDecodeError:
                    pass
            print(f"  NSE {symbol} attempt {attempt+1}: HTTP {r.status_code}")
        except Exception as e:
            print(f"  NSE {symbol} attempt {attempt+1} error: {e}")
        time.sleep(3 * (attempt + 1))
    return None


def parse_option_chain(raw):
    if not raw:
        return None
    records = raw.get("records", {})
    data = records.get("data", [])
    underlying = safe_float(records.get("underlyingValue"))
    expiry_dates = records.get("expiryDates", [])
    nearest_expiry = expiry_dates[0] if expiry_dates else None
    strikes = {}
    for row in data:
        if row.get("expiryDate") != nearest_expiry:
            continue
        sp = row.get("strikePrice")
        if sp is None:
            continue
        entry = {"strike": sp}
        for side in ("CE", "PE"):
            if row.get(side):
                leg = row[side]
                entry[side] = {
                    "ltp": safe_float(leg.get("lastPrice")),
                    "oi": safe_float(leg.get("openInterest")),
                    "iv": safe_float(leg.get("impliedVolatility")),
                    "bid": safe_float(leg.get("bidprice")),
                    "ask": safe_float(leg.get("askPrice")),
                }
        strikes[str(sp)] = entry
    return {"underlying": underlying, "expiry": nearest_expiry,
            "strikes": strikes, "ts": int(time.time())}


NEWS_SOURCES = [
    ("MoneyControl", "https://www.moneycontrol.com/rss/marketsnews.xml"),
    ("ET Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("LiveMint Markets", "https://www.livemint.com/rss/markets"),
    ("CNBC TV18", "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml"),
    ("Google India News", "https://news.google.com/rss/search?q=india+stock+market+when:1h&hl=en-IN&gl=IN&ceid=IN:en"),
]

def fetch_rss(name, url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        items = []
        for it in root.iter("item"):
            title = it.findtext("title", "").strip()
            link = it.findtext("link", "").strip()
            pubdate = it.findtext("pubDate", "").strip()
            if title:
                items.append({"src": name, "title": title, "link": link, "pubDate": pubdate})
            if len(items) >= 5:
                break
        return items
    except Exception as e:
        print(f"  RSS {name} failed: {e}")
        return []


def fetch_all_news():
    all_items = []
    for name, url in NEWS_SOURCES:
        items = fetch_rss(name, url)
        all_items.extend(items)
        print(f"  News {name}: {len(items)} headlines")
        time.sleep(0.3)
    return all_items[:30]


def safe_dump(obj, path):
    """Write JSON with allow_nan=False so any sneaky NaN/Inf raises an error
    rather than producing invalid JSON the frontend can't parse."""
    text = json.dumps(obj, indent=2, allow_nan=False)
    Path(path).write_text(text)


def main():
    print(f"=== Fetch run @ {datetime.now(IST).isoformat()} ===")

    print("\n[1/4] Yahoo Finance via yfinance library…")
    quotes = fetch_all_yf()

    print("\n[2/4] Fallbacks…")
    if "USDINR" not in quotes:
        fx = fetch_frankfurter_fx()
        if fx:
            quotes["USDINR"] = fx
            print(f"  ✓ USDINR fallback {fx['price']:.4f}")
    if "BTC" not in quotes or "ETH" not in quotes:
        cg = fetch_coingecko()
        for k, v in cg.items():
            if k not in quotes:
                quotes[k] = v
                print(f"  ✓ {k} fallback")

    print("\n[3/4] NSE Option Chain…")
    nifty_oc = fetch_nse_option_chain("NIFTY")
    bn_oc = fetch_nse_option_chain("BANKNIFTY")
    option_chain = {
        "NIFTY": parse_option_chain(nifty_oc) if nifty_oc else None,
        "BANKNIFTY": parse_option_chain(bn_oc) if bn_oc else None,
        "fetchedAt": int(time.time()),
    }
    print(f"  NIFTY chain: {'OK' if option_chain['NIFTY'] else 'FAILED'}")
    print(f"  BANKNIFTY chain: {'OK' if option_chain['BANKNIFTY'] else 'FAILED'}")

    print("\n[4/4] News RSS…")
    news = fetch_all_news()
    print(f"  Total: {len(news)} headlines")

    snapshot = {
        "fetchedAt": int(time.time()),
        "fetchedAtIso": datetime.now(IST).isoformat(),
        "quotes": quotes,
    }
    safe_dump(snapshot, DATA_DIR / "snapshot.json")
    safe_dump(option_chain, DATA_DIR / "option_chain.json")
    safe_dump({"items": news, "fetchedAt": int(time.time())}, DATA_DIR / "news.json")

    print(f"\n✓ Wrote data/snapshot.json ({len(quotes)} quotes)")
    print(f"✓ Wrote data/option_chain.json")
    print(f"✓ Wrote data/news.json ({len(news)} items)")


if __name__ == "__main__":
    main()
