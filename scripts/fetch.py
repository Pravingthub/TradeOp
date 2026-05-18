#!/usr/bin/env python3
"""
The Trade — Server-side data fetcher v19.5
Adds anti-regression merge: if yfinance returns older data than what's already in snapshot.json,
the existing newer data is preserved per-symbol. Prevents transient yfinance hiccups from
rolling back known-good data (e.g. NSE Asian markets sometimes lose recent days).

Returns TWO sessions per symbol:
- todaySession: most recent bar (today's if available)
- priorSession: the bar before that

Each session includes date so frontend knows which day's data it has.

Frontend picks based on mode:
- Pre-Market (planning tomorrow) -> uses todaySession for pivots
- Live (trading today) -> uses priorSession for pivots
"""

import json
import math
import time
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


def round_session(s):
    """Round all numeric fields in a session dict to 4 decimal places."""
    if not s:
        return s
    out = {}
    for k, v in s.items():
        if isinstance(v, float):
            out[k] = round(v, 4)
        else:
            out[k] = v
    return out


def fetch_yf(symbol, key):
    """Returns price + today's session OHLC + prior session OHLC."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="15d", auto_adjust=False)
        if hist is None or len(hist) < 2:
            return None
        hist = hist.dropna(subset=["Close"])
        if len(hist) < 2:
            return None

        latest = hist.iloc[-1]
        prior = hist.iloc[-2]

        latest_close = safe_float(latest["Close"])
        prior_close = safe_float(prior["Close"])
        if not latest_close or not prior_close or latest_close <= 0 or prior_close <= 0:
            return None

        try:
            latest_date = hist.index[-1].strftime("%Y-%m-%d")
            prior_date = hist.index[-2].strftime("%Y-%m-%d")
        except Exception:
            latest_date = prior_date = None

        today_session = round_session({
            "open": safe_float(latest["Open"]),
            "high": safe_float(latest["High"]),
            "low": safe_float(latest["Low"]),
            "close": latest_close,
            "date": latest_date,
        })
        prior_session = round_session({
            "open": safe_float(prior["Open"]),
            "high": safe_float(prior["High"]),
            "low": safe_float(prior["Low"]),
            "close": prior_close,
            "date": prior_date,
        })

        return {
            "price": round(latest_close, 4),
            "prev": round(prior_close, 4),
            "change": round(latest_close - prior_close, 4),
            "pct": round((latest_close - prior_close) / prior_close * 100, 3),
            "todaySession": today_session,
            "priorSession": prior_session,
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
            t = v.get("todaySession") or {}
            p = v.get("priorSession") or {}
            print(f"  OK {key:10s} {v['price']:>12,.{dec}f}  ({v['pct']:+.2f}%)")
            print(f"     today  {t.get('date')}: H={t.get('high')} L={t.get('low')} C={t.get('close')}")
            print(f"     prior  {p.get('date')}: H={p.get('high')} L={p.get('low')} C={p.get('close')}")
        else:
            print(f"  -- {key:10s} skipped (no valid data)")
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
                "todaySession": {"open": rate, "high": rate, "low": rate, "close": rate, "date": None},
                "priorSession": {"open": rate, "high": rate, "low": rate, "close": rate, "date": None},
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
                    dec = 0 if ok == "BTC" else 2
                    out[ok] = {
                        "price": round(price, dec),
                        "prev": round(prev, dec),
                        "change": round(price - prev, dec),
                        "pct": round(pct, 3),
                        "todaySession": {"open": round(price, dec), "high": round(price, dec),
                                         "low": round(price, dec), "close": round(price, dec), "date": None},
                        "priorSession": {"open": round(prev, dec), "high": round(prev, dec),
                                         "low": round(prev, dec), "close": round(prev, dec), "date": None},
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
    text = json.dumps(obj, indent=2, allow_nan=False)
    Path(path).write_text(text)


def merge_with_existing(new_quotes, existing_path):
    """
    Anti-regression merge: if existing snapshot has NEWER session data
    than new fetch (per symbol), keep the existing one.
    Prevents yfinance hiccups from rolling back known-good data.
    """
    if not existing_path.exists():
        return new_quotes, []

    try:
        existing = json.loads(existing_path.read_text())
        existing_quotes = existing.get("quotes", {})
    except Exception as e:
        print(f"  Could not read existing snapshot ({e}), using new data")
        return new_quotes, []

    merged = {}
    kept_symbols = []
    for sym, new_data in new_quotes.items():
        new_today_date = (new_data.get("todaySession") or {}).get("date")
        old_data = existing_quotes.get(sym)
        if not old_data:
            merged[sym] = new_data
            continue
        old_today_date = (old_data.get("todaySession") or {}).get("date")

        # If old has newer or equal date, AND old has H/L/C, keep old.
        # This prevents regression but allows updates within the same trading day
        # (e.g. updated close after market hours).
        if old_today_date and new_today_date:
            if old_today_date > new_today_date:
                # Old data is newer — KEEP OLD (regression protection)
                merged[sym] = old_data
                kept_symbols.append(f"{sym}({old_today_date} kept, new was {new_today_date})")
                continue
            elif old_today_date == new_today_date:
                # Same date — prefer the new data (it may have updated close)
                merged[sym] = new_data
                continue
        merged[sym] = new_data

    # Also retain symbols that existed in old but are missing from new (rare network failure)
    for sym, old_data in existing_quotes.items():
        if sym not in merged:
            merged[sym] = old_data
            kept_symbols.append(f"{sym}(retained — missing from new fetch)")

    return merged, kept_symbols


def main():
    print(f"=== Fetch run @ {datetime.now(IST).isoformat()} ===")
    print("\n[1/4] Yahoo Finance via yfinance library...")
    quotes = fetch_all_yf()

    print("\n[2/4] Fallbacks...")
    if "USDINR" not in quotes:
        fx = fetch_frankfurter_fx()
        if fx:
            quotes["USDINR"] = fx
            print(f"  OK USDINR fallback")
    if "BTC" not in quotes or "ETH" not in quotes:
        cg = fetch_coingecko()
        for k, v in cg.items():
            if k not in quotes:
                quotes[k] = v
                print(f"  OK {k} fallback")

    # Anti-regression: never let newer-than-fetch data get overwritten by older
    print("\n[2.5/4] Anti-regression merge with existing snapshot...")
    snapshot_path = DATA_DIR / "snapshot.json"
    quotes, kept = merge_with_existing(quotes, snapshot_path)
    if kept:
        print(f"  Preserved {len(kept)} symbol(s) from existing snapshot:")
        for k in kept:
            print(f"    - {k}")
    else:
        print(f"  All new data accepted (no regressions detected)")

    print("\n[3/4] NSE Option Chain...")
    nifty_oc = fetch_nse_option_chain("NIFTY")
    bn_oc = fetch_nse_option_chain("BANKNIFTY")
    option_chain = {
        "NIFTY": parse_option_chain(nifty_oc) if nifty_oc else None,
        "BANKNIFTY": parse_option_chain(bn_oc) if bn_oc else None,
        "fetchedAt": int(time.time()),
    }
    print(f"  NIFTY chain: {'OK' if option_chain['NIFTY'] else 'FAILED'}")
    print(f"  BANKNIFTY chain: {'OK' if option_chain['BANKNIFTY'] else 'FAILED'}")

    print("\n[4/4] News RSS...")
    news = fetch_all_news()
    print(f"  Total: {len(news)} headlines")

    snapshot = {
        "fetchedAt": int(time.time()),
        "fetchedAtIso": datetime.now(IST).isoformat(),
        "quotes": quotes,
    }
    safe_dump(snapshot, snapshot_path)
    safe_dump(option_chain, DATA_DIR / "option_chain.json")
    safe_dump({"items": news, "fetchedAt": int(time.time())}, DATA_DIR / "news.json")

    print(f"\nOK Wrote data/snapshot.json ({len(quotes)} quotes)")
    print(f"OK Wrote data/option_chain.json")
    print(f"OK Wrote data/news.json ({len(news)} items)")


if __name__ == "__main__":
    main()
