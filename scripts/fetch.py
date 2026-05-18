#!/usr/bin/env python3
"""
The Trade — Server-side data fetcher v19.1
Uses Stooq + Frankfurter + CoinGecko instead of Yahoo Finance (which blocks GitHub IPs).
Runs in GitHub Actions every 10 minutes.
"""

import json
import time
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
import xml.etree.ElementTree as ET

IST = timezone(timedelta(hours=5, minutes=30))
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ─────────────────────────────────────────────────────────
# STOOQ — primary source for indices + commodities
# Stooq returns daily CSV. Confirmed works from GitHub Actions.
# ─────────────────────────────────────────────────────────

# Stooq symbols. ^nsei = Nifty 50, ^nseb = Bank Nifty
STOOQ_SYMBOLS = {
    # Indian
    "NIFTY":     "^nsei",
    "BANKNIFTY": "^nseb",
    # US indices
    "SP500":     "^spx",
    "NASDAQ":    "^ndq",
    "DOW":       "^dji",
    # Europe
    "DAX":       "^dax",
    "FTSE":      "^ftm",
    "CAC":       "^cac",
    # Asia
    "NIKKEI":    "^nkx",
    "HSI":       "^hsi",
    "SHCOMP":    "^shc",
    # Dollar
    "DXY":       "^dxy",
    # Commodities (Stooq uses these tickers)
    "BRENT":     "cb.f",   # Brent futures continuous
    "WTI":       "cl.f",   # WTI futures
    "GOLD":      "gc.f",   # Gold futures
    "SILVER":    "si.f",   # Silver futures
}

def fetch_stooq_quote(symbol):
    """Fetch from Stooq CSV. Returns latest + previous day OHLC."""
    # Historical CSV for last 10 days — gives us today + prev
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200 or len(r.text) < 50:
            return None
        lines = r.text.strip().split("\n")
        if len(lines) < 3:  # need at least header + 2 rows
            return None
        # Header: Date,Open,High,Low,Close,Volume
        # Take last 2 rows (today, yesterday)
        latest = lines[-1].split(",")
        prev = lines[-2].split(",")
        try:
            price = float(latest[4])  # today's close
            prev_close = float(prev[4])
            prev_high = float(prev[2])
            prev_low = float(prev[3])
            prev_open = float(prev[1])
            return {
                "price": round(price, 2),
                "prev": round(prev_close, 2),
                "change": round(price - prev_close, 2),
                "pct": round((price - prev_close) / prev_close * 100, 3),
                "prevHigh": round(prev_high, 2),
                "prevLow": round(prev_low, 2),
                "prevOpen": round(prev_open, 2),
                "ts": int(time.time()),
                "source": "stooq",
            }
        except (ValueError, IndexError):
            return None
    except Exception as e:
        print(f"  Stooq fetch error {symbol}: {e}")
        return None


def fetch_stooq_intraday(symbol):
    """Stooq intraday — uses 'light' quote endpoint. Fallback for indices that need fresher data."""
    url = f"https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return None
        lines = r.text.strip().split("\n")
        if len(lines) < 2:
            return None
        hdr = lines[0].split(",")
        vals = lines[1].split(",")
        d = dict(zip(hdr, vals))
        try:
            return {
                "open": float(d.get("Open", 0)) if d.get("Open") and d.get("Open") != "N/D" else None,
                "high": float(d.get("High", 0)) if d.get("High") and d.get("High") != "N/D" else None,
                "low":  float(d.get("Low", 0)) if d.get("Low") and d.get("Low") != "N/D" else None,
                "close": float(d.get("Close", 0)) if d.get("Close") and d.get("Close") != "N/D" else None,
                "date": d.get("Date", ""),
            }
        except ValueError:
            return None
    except Exception:
        return None


def fetch_all_stooq():
    out = {}
    for key, sym in STOOQ_SYMBOLS.items():
        v = fetch_stooq_quote(sym)
        if v:
            out[key] = v
            print(f"  ✓ {key:10s} {v['price']:>12,.2f}  ({v['pct']:+.2f}%)")
        else:
            print(f"  ✗ {key:10s} FAILED")
        time.sleep(0.3)
    return out


# ─────────────────────────────────────────────────────────
# FRANKFURTER — FX rates from ECB. Free, no auth, no rate limits.
# ─────────────────────────────────────────────────────────

def fetch_fx():
    """Fetch USD/INR from Frankfurter. Computes pct change vs yesterday."""
    out = {}
    try:
        # Latest USD->INR
        r = requests.get("https://api.frankfurter.app/latest?from=USD&to=INR",
                         headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return out
        j = r.json()
        today_rate = j.get("rates", {}).get("INR")
        date_today = j.get("date")

        # Yesterday's rate
        # Frankfurter doesn't support 'yesterday' directly; use last 2 days
        r2 = requests.get(f"https://api.frankfurter.app/{date_today}..?from=USD&to=INR",
                          headers=HEADERS, timeout=8)
        if r2.status_code == 200:
            j2 = r2.json()
            dates = sorted(j2.get("rates", {}).keys())
            if len(dates) >= 2:
                prev_rate = j2["rates"][dates[-2]]["INR"]
                out["USDINR"] = {
                    "price": round(today_rate, 4),
                    "prev": round(prev_rate, 4),
                    "change": round(today_rate - prev_rate, 4),
                    "pct": round((today_rate - prev_rate) / prev_rate * 100, 3),
                    "source": "frankfurter",
                    "ts": int(time.time()),
                }
                print(f"  ✓ USDINR     {today_rate:>12,.4f}  ({out['USDINR']['pct']:+.2f}%)")
            else:
                out["USDINR"] = {
                    "price": round(today_rate, 4),
                    "prev": round(today_rate, 4),
                    "change": 0,
                    "pct": 0,
                    "source": "frankfurter",
                    "ts": int(time.time()),
                }
                print(f"  ✓ USDINR     {today_rate:>12,.4f}  (no prev)")
    except Exception as e:
        print(f"  ✗ USDINR FX fetch failed: {e}")
    return out


# ─────────────────────────────────────────────────────────
# COINGECKO — crypto, free, no auth, CORS-friendly
# ─────────────────────────────────────────────────────────

def fetch_crypto():
    out = {}
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return out
        j = r.json()
        btc = j.get("bitcoin", {})
        eth = j.get("ethereum", {})
        if btc.get("usd"):
            out["BTC"] = {
                "price": round(btc["usd"], 0),
                "prev": round(btc["usd"] / (1 + btc.get("usd_24h_change", 0)/100), 0) if btc.get("usd_24h_change") else round(btc["usd"], 0),
                "change": round(btc["usd"] * btc.get("usd_24h_change", 0) / 100, 0),
                "pct": round(btc.get("usd_24h_change", 0), 3),
                "source": "coingecko",
                "ts": int(time.time()),
            }
            print(f"  ✓ BTC        {btc['usd']:>12,.0f}  ({out['BTC']['pct']:+.2f}%)")
        if eth.get("usd"):
            out["ETH"] = {
                "price": round(eth["usd"], 2),
                "prev": round(eth["usd"] / (1 + eth.get("usd_24h_change", 0)/100), 2) if eth.get("usd_24h_change") else round(eth["usd"], 2),
                "change": round(eth["usd"] * eth.get("usd_24h_change", 0) / 100, 2),
                "pct": round(eth.get("usd_24h_change", 0), 3),
                "source": "coingecko",
                "ts": int(time.time()),
            }
            print(f"  ✓ ETH        {eth['usd']:>12,.2f}  ({out['ETH']['pct']:+.2f}%)")
    except Exception as e:
        print(f"  ✗ Crypto fetch failed: {e}")
    return out


# ─────────────────────────────────────────────────────────
# INDIA VIX — scrape from Moneycontrol or Investing
# ─────────────────────────────────────────────────────────

def fetch_india_vix():
    """Try multiple sources for India VIX."""
    # Try Moneycontrol first
    try:
        url = "https://www.moneycontrol.com/indian-indices/india-vix-36.html"
        r = requests.get(url, headers={**HEADERS, "Accept": "text/html"}, timeout=10)
        if r.status_code == 200:
            html = r.text
            # Try to find the price using regex on common patterns
            # Moneycontrol has data-something="<price>" attributes
            m = re.search(r'"lastprice"[:\s]*"?(\d+\.?\d*)"?', html)
            prev_m = re.search(r'"prevclose"[:\s]*"?(\d+\.?\d*)"?', html)
            if m:
                price = float(m.group(1))
                prev = float(prev_m.group(1)) if prev_m else price
                if price > 5 and price < 100:  # sanity check
                    return {
                        "price": round(price, 2),
                        "prev": round(prev, 2),
                        "change": round(price - prev, 2),
                        "pct": round((price - prev) / prev * 100, 3) if prev else 0,
                        "source": "moneycontrol",
                        "ts": int(time.time()),
                    }
    except Exception as e:
        print(f"  Moneycontrol VIX scrape failed: {e}")
    return None


# ─────────────────────────────────────────────────────────
# NSE OPTION CHAIN — best effort
# ─────────────────────────────────────────────────────────

def fetch_nse_option_chain(symbol, max_retries=3):
    """NSE with persistent session + retries. Often blocked from GH but worth trying."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.nseindia.com/option-chain",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    })
    for attempt in range(max_retries):
        try:
            # Cookie warmup chain
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
    underlying = records.get("underlyingValue")
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
        if row.get("CE"):
            ce = row["CE"]
            entry["CE"] = {
                "ltp": ce.get("lastPrice"),
                "oi": ce.get("openInterest"),
                "iv": ce.get("impliedVolatility"),
                "bid": ce.get("bidprice"),
                "ask": ce.get("askPrice"),
            }
        if row.get("PE"):
            pe = row["PE"]
            entry["PE"] = {
                "ltp": pe.get("lastPrice"),
                "oi": pe.get("openInterest"),
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
# RSS NEWS
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


# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────

def main():
    print(f"=== Fetch run @ {datetime.now(IST).isoformat()} ===")

    print("\n[1/4] Stooq quotes (indices + commodities)…")
    quotes = fetch_all_stooq()

    print("\n[2/4] FX + Crypto…")
    fx = fetch_fx()
    quotes.update(fx)
    crypto = fetch_crypto()
    quotes.update(crypto)

    print("\n[2b/4] India VIX scrape…")
    vix = fetch_india_vix()
    if vix:
        quotes["INDIAVIX"] = vix
        print(f"  ✓ INDIAVIX   {vix['price']:>12,.2f}  ({vix['pct']:+.2f}%)")
    else:
        print(f"  ✗ INDIAVIX failed (fallback: estimate from Nifty range)")

    print("\n[3/4] NSE Option Chain…")
    nifty_oc = fetch_nse_option_chain("NIFTY")
    bn_oc = fetch_nse_option_chain("BANKNIFTY")
    option_chain = {
        "NIFTY": parse_option_chain(nifty_oc) if nifty_oc else None,
        "BANKNIFTY": parse_option_chain(bn_oc) if bn_oc else None,
        "fetchedAt": int(time.time()),
    }
    print(f"  NIFTY chain: {'OK' if option_chain['NIFTY'] else 'FAILED (NSE blocks GH IPs — will retry, mobile fetch may work)'}")
    print(f"  BANKNIFTY chain: {'OK' if option_chain['BANKNIFTY'] else 'FAILED'}")

    print("\n[4/4] News RSS…")
    news = fetch_all_news()
    print(f"  Total: {len(news)} headlines")

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
