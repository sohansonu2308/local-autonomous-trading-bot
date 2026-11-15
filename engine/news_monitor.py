import hashlib
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple
from engine.database import get_conn

RSS_FEEDS = [
    ("EconomicTimes", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/MCtopnews.xml"),
    ("LiveMint", "https://www.livemint.com/rss/markets"),
]

BULL_WORDS = {
    "rally", "surge", "gain", "bull", "buy", "breakout", "record", "high",
    "inflow", "fii buying", "upgrade", "profit", "beat", "strong", "boost",
}
BEAR_WORDS = {
    "fall", "drop", "crash", "slump", "sell", "fii selling", "outflow",
    "downgrade", "loss", "weak", "inflation", "deficit", "tariff", "war", "fear",
}

SYMBOL_KEYWORDS = {
    "NIFTY50": ["nifty", "nse", "sensex", "indian market", "dalal street", "fii", "dii", "rbi"],
    "BANKNIFTY": ["bank nifty", "banknifty", "rbi", "banking", "rate cut", "repo rate", "lender"],
    "RELIANCE": ["reliance", "ril", "jio", "ambani", "refining"],
    "HDFCBANK": ["hdfc bank", "hdfc"],
    "ICICIBANK": ["icici bank", "icici"],
    "SBIN": ["sbi", "state bank"],
    "TCS": ["tcs", "tata consultancy", "it stocks"],
    "INFY": ["infosys", "infy", "it stocks"],
    "GOLD": ["gold", "bullion", "precious metal", "mcx gold", "safe haven"],
    "SILVER": ["silver", "bullion", "mcx silver"],
    "CRUDEOIL": ["crude", "oil", "brent", "wtl", "opec", "petroleum"],
    "NATURALGAS": ["natural gas", "lng", "gas"],
}


def _classify_headline(title: str) -> Tuple[str, str]:
    low = title.lower()
    tag = "MARKET_MACRO"
    for sym, kws in SYMBOL_KEYWORDS.items():
        if any(k in low for k in kws):
            tag = sym
            break

    bull_hits = sum(1 for w in BULL_WORDS if w in low)
    bear_hits = sum(1 for w in BEAR_WORDS if w in low)
    if bull_hits > bear_hits:
        sentiment = "BULLISH_CATALYST"
    elif bear_hits > bull_hits:
        sentiment = "BEARISH_RISK"
    else:
        sentiment = "NEUTRAL_FLOW"
    return tag, sentiment


def fetch_and_store_crypto_news(max_items: int = 20) -> int:
    conn = get_conn()
    cur = conn.cursor()
    now_iso = datetime.now(timezone.utc).isoformat()
    inserted = 0

    for source_name, url in RSS_FEEDS:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                xml_bytes = resp.read()
            root = ET.fromstring(xml_bytes)
            items = root.findall(".//item")[:max_items]
            for item in items:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub = (item.findtext("pubDate") or now_iso).strip()
                if not title:
                    continue
                h = hashlib.sha256(title.lower().encode("utf-8")).hexdigest()[:24]
                sym_tag, sentiment = _classify_headline(title)
                try:
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO news_items (
                            item_hash, source, title, link, symbol_tag, sentiment, published_at, first_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (h, source_name, title, link, sym_tag, sentiment, pub, now_iso),
                    )
                    if cur.rowcount > 0:
                        inserted += 1
                except Exception:
                    pass
        except Exception:
            pass

    conn.commit()
    conn.close()
    return inserted


def get_recent_news_for_symbol(symbol: str, limit: int = 3) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT source, title, symbol_tag, sentiment, published_at
        FROM news_items
        WHERE symbol_tag IN (?, 'NIFTY50', 'MARKET_MACRO')
        ORDER BY id DESC LIMIT ?
        """,
        (symbol.upper(), limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows
