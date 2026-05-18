#!/usr/bin/env python3
"""
The Trade — Server-side data fetcher.
Runs in GitHub Actions every 10 minutes. Fetches Yahoo Finance, NSE option chain,
and RSS news. Writes JSON to data/ folder. No API keys required.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
import xml.etree.ElementTree as ET

IST = timezone(timedelta(hours=5, minutes=30))

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# Realistic browser headers — Yahoo + NSE require these
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

# ─────────────────────────────────────────────────────────
# YAHOO FINANCE — primary source for all indices + globals
# ─────────────────────────────────────────────────────────

YAHOO_SYMBOLS = {
    # Indian
    "NIFTY":      "%5ENSEI",
    "BANKNIFTY":  "%5ENSEBANK",
    "INDIAVIX":   "%5EINDIAVIX",
    # Global indices
    "SP500":      "%5EGSPC",
    "NASDAQ":     "%5EIXIC",
    "DOW":        "%5EDJI",
    "DAX":        "%5EGDAXI",
    "NIKKEI":     "%5EN225",
    "FTSE":       "%5EFTSE",
    "HSI":        "%5EHSI",       # Hang Seng
    "SHCOMP":     "000001.SS",     # Shanghai
    # FX
    "USDINR":     "INR%3DX",
    "EURUSD":     "EURUSD%3DX",
    "DXY":        "DX-Y.NYB",      # Dollar index
    # Commodities
    "BRENT":      "BZ%3DF",
    "WTI":        "CL%3DF",
    "GOLD":       "GC%3DF",
    "SILVER":     "SI%3DF",
    # Crypto
    "BTC":        "BTC-USD",
    "ETH":        "ETH-USD",
}

def fetch_yahoo(symbol):
    """Fetch a single Yahoo Finance symbol — returns price, prev, %, OHLC."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=10d"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        j = r.json()
        result = j.get("chart", {}).get("result", [{}])[0]
        meta = result.get("meta", {})
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if not price or not prev:
            return None
        
        # Get prev day OHLC from indicators
        prev_high = prev_low = prev_open = None
        try:
            quotes = result.get("indicators", {}).get("quote", [{}])[0]
            timestamps = result.get("timestamp", [])
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            for i in range(len(timestamps) - 1, -1, -1):
                ts_date = datetime.fromtimestamp(timestamps[i], IST).strftime("%Y-%m-%d")
                if ts_date < today_str:
                    h = quotes.get("high", [None])[i]
                    l = quotes.get("low", [None])[i]
                    o = quotes.get("open", [None])[i]
                    if h and l and o:
                        prev_high, prev_low, prev_open = h, l, o
                        break
        except Exception:
            pass
        
        return {
            "price": round(float(price), 2),
            "prev": round(float(prev), 2),
            "change": round(float(price - prev), 2),
            "pct": round(float((price - prev) / prev * 100), 3),
            "prevHigh": round(float(prev_high), 2) if prev_high else None,
            "prevLow": round(float(prev_low), 2) if prev_low else None,
            "prevOpen": round(float(prev_open), 2) if prev_open else None,
            "ts": int(time.time()),
        }
    except Exception as e:
        print(f"  Yahoo fetch failed for {symbol}: {e}")
        return None


def fetch_all_yahoo():
    """Fetch all Yahoo symbols with delay between requests."""
    out = {}
    for key, sym in YAHOO_SYMBOLS.items():
        v = fetch_yahoo(sym)
        if v:
            out[key] = v
            print(f"  ✓ {key:10s} {v['price']:>12,.2f}  ({v['pct']:+.2f}%)")
        else:
            print(f"  ✗ {key:10s} FAILED")
        time.sleep(0.5)  # Be polite to Yahoo
    return out


# ─────────────────────────────────────────────────────────
# STOOQ — fallback / cross-check for Indian indices
# ─────────────────────────────────────────────────────────

def fetch_stooq(symbol):
    """Fetch Stooq CSV — returns dict with OHLC."""
    url = f"https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return None
        lines = r.text.strip().split("\n")
        if len(lines) < 2:
            return None
        headers_csv = lines[0].split(",")
        values = lines[1].split(",")
        d = dict(zip(headers_csv, values))
        try:
            return {
                "open": float(d.get("Open", 0)),
                "high": float(d.get("High", 0)),
                "low": float(d.get("Low", 0)),
                "close": float(d.get("Close", 0)),
                "date": d.get("Date", ""),
            }
        except ValueError:
            return None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────
# NSE OPTION CHAIN
# ─────────────────────────────────────────────────────────

def fetch_nse_option_chain(symbol):
    """Fetch NSE option chain. Requires cookie warmup."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.nseindia.com/option-chain",
        "Connection": "keep-alive",
    })
    try:
        # Step 1: Warmup — visit homepage to get cookies
        session.get("https://www.nseindia.com/", timeout=10)
        time.sleep(1)
        session.get("https://www.nseindia.com/option-chain", timeout=10)
        time.sleep(1)
        # Step 2: Fetch option chain
        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
        r = session.get(url, timeout=15)
        if r.status_code != 200:
            print(f"  NSE option chain {symbol}: HTTP {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        print(f"  NSE option chain fetch failed for {symbol}: {e}")
        return None


def parse_option_chain(raw):
    """Parse NSE option chain into compact strike->{CE,PE} map."""
    if not raw:
        return None
    records = raw.get("records", {})
    data = records.get("data", [])
    underlying = records.get("underlyingValue")
    expiry_dates = records.get("expiryDates", [])
    # Use nearest expiry
    nearest_expiry = expiry_dates[0] if expiry_dates else None
    
    strikes = {}
    for row in data:
        if row.get("expiryDate") != nearest_expiry:
            continue
        sp = row.get("strikePrice")
        if sp is None:
            continue
        entry = {"strike": sp}
        if row.get("CE"):
            ce = row["CE"]
            entry["CE"] = {
                "ltp": ce.get("lastPrice"),
                "oi": ce.get("openInterest"),
                "chgOI": ce.get("changeinOpenInterest"),
                "vol": ce.get("totalTradedVolume"),
                "iv": ce.get("impliedVolatility"),
                "bid": ce.get("bidprice"),
                "ask": ce.get("askPrice"),
            }
        if row.get("PE"):
            pe = row["PE"]
            entry["PE"] = {
                "ltp": pe.get("lastPrice"),
                "oi": pe.get("openInterest"),
                "chgOI": pe.get("changeinOpenInterest"),
                "vol": pe.get("totalTradedVolume"),
                "iv": pe.get("impliedVolatility"),
                "bid": pe.get("bidprice"),
                "ask": pe.get("askPrice"),
            }
        strikes[str(sp)] = entry
    
    return {
        "underlying": underlying,
        "expiry": nearest_expiry,
        "strikes": strikes,
        "ts": int(time.time()),
    }


# ─────────────────────────────────────────────────────────
# RSS NEWS — multiple sources
# ─────────────────────────────────────────────────────────

NEWS_SOURCES = [
    ("MoneyControl", "https://www.moneycontrol.com/rss/marketsnews.xml"),
    ("ET Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("LiveMint Markets", "https://www.livemint.com/rss/markets"),
    ("CNBC TV18", "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml"),
    ("Reuters India", "https://news.google.com/rss/search?q=india+stock+market+when:1h&hl=en-IN&gl=IN&ceid=IN:en"),
]

def fetch_rss(name, url):
    """Fetch one RSS feed, return list of items."""
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
        print(f"  RSS fetch failed for {name}: {e}")
        return []


def fetch_all_news():
    all_items = []
    for name, url in NEWS_SOURCES:
        items = fetch_rss(name, url)
        all_items.extend(items)
        print(f"  News {name}: {len(items)} headlines")
        time.sleep(0.3)
    # Sort by pubDate (rough — strings work for ISO-ish dates)
    return all_items[:30]


# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────

def main():
    print(f"=== Fetch run @ {datetime.now(IST).isoformat()} ===")
    
    print("\n[1/3] Yahoo Finance…")
    quotes = fetch_all_yahoo()
    
    print("\n[2/3] NSE Option Chain…")
    nifty_oc = fetch_nse_option_chain("NIFTY")
    bn_oc = fetch_nse_option_chain("BANKNIFTY")
    option_chain = {
        "NIFTY": parse_option_chain(nifty_oc) if nifty_oc else None,
        "BANKNIFTY": parse_option_chain(bn_oc) if bn_oc else None,
        "fetchedAt": int(time.time()),
    }
    print(f"  NIFTY chain: {'OK' if option_chain['NIFTY'] else 'FAILED'}")
    print(f"  BANKNIFTY chain: {'OK' if option_chain['BANKNIFTY'] else 'FAILED'}")
    
    print("\n[3/3] News RSS…")
    news = fetch_all_news()
    print(f"  Total: {len(news)} headlines")
    
    # Write snapshot
    snapshot = {
        "fetchedAt": int(time.time()),
        "fetchedAtIso": datetime.now(IST).isoformat(),
        "quotes": quotes,
    }
    (DATA_DIR / "snapshot.json").write_text(json.dumps(snapshot, indent=2))
    (DATA_DIR / "option_chain.json").write_text(json.dumps(option_chain, indent=2))
    (DATA_DIR / "news.json").write_text(json.dumps({"items": news, "fetchedAt": int(time.time())}, indent=2))
    
    print(f"\n✓ Wrote data/snapshot.json ({len(quotes)} quotes)")
    print(f"✓ Wrote data/option_chain.json")
    print(f"✓ Wrote data/news.json ({len(news)} items)")


if __name__ == "__main__":
    main()
