import hashlib
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List, Dict, Any
from engine.database import get_conn

CRYPTO_RSS_FEEDS = [
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
]

BULLISH_WORDS = {
    "etf", "approval", "inflow", "surge", "breakout", "institutional",
    "accumulation", "adoption", "partnership", "launch", "upgrade", "bullish",
    "record", "rally", "treasury", "buy",
}

BEARISH_WORDS = {
    "hack", "exploit", "sec", "lawsuit", "outflow", "crash", "dump",
    "liquidation", "ban", "investigation", "bankruptcy", "delist", "unlock",
    "sell-off", "bearish", "warning",
}

COIN_KEYWORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth", "vitalik"],
    "SOL": ["solana", "sol"],
    "XRP": ["xrp", "ripple"],
    "DOGE": ["dogecoin", "doge"],
    "SUI": ["sui"],
    "BNB": ["bnb", "binance"],
}


def _classify_headline(title: str) -> Dict[str, str]:
    lower = title.lower()
    matched_tag = "MACRO/CRYPTO"
    for sym, words in COIN_KEYWORDS.items():
        if any(f" {w}" in f" {lower} " or f"{w} " in f"{lower} " for w in words):
            matched_tag = f"{sym}USDT"
            break

    bull_hits = sum(1 for w in BULLISH_WORDS if w in lower)
    bear_hits = sum(1 for w in BEARISH_WORDS if w in lower)

    if bull_hits > bear_hits:
        sentiment = "BULLISH_CATALYST"
    elif bear_hits > bull_hits:
        sentiment = "BEARISH_RISK"
    else:
        sentiment = "NEUTRAL_HEADLINE"

    return {"symbol_tag": matched_tag, "sentiment": sentiment}


def fetch_and_store_crypto_news(max_items: int = 20) -> List[Dict[str, Any]]:
    """
    Stateful deduplicated RSS news monitor (adapted from trader-news/news.py).
    Fetches live breaking crypto headlines, hashes them into SQLite so each item
    is stored once, and tags coin & catalyst sentiment.
    """
    conn = get_conn()
    cur = conn.cursor()
    now_iso = datetime.now(timezone.utc).isoformat()

    for source_name, url in CRYPTO_RSS_FEEDS:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WhaleQuantNews/2.0",
                    "Accept": "application/rss+xml, application/xml, text/xml",
                },
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                xml_bytes = resp.read()
            root = ET.fromstring(xml_bytes)
            items = root.findall(".//item")[:10]
            for item in items:
                title_el = item.find("title")
                link_el = item.find("link")
                pub_el = item.find("pubDate")
                if title_el is None or not title_el.text:
                    continue
                title = title_el.text.strip()
                link = (link_el.text or "").strip() if link_el is not None else ""
                pub_date = (pub_el.text or now_iso).strip() if pub_el is not None else now_iso

                item_hash = hashlib.sha256(f"{title}_{source_name}".encode("utf-8")).hexdigest()[:16]
                meta = _classify_headline(title)

                cur.execute(
                    """
                    INSERT OR IGNORE INTO news_items (
                        item_hash, source, title, link, symbol_tag, sentiment, published_at, first_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_hash,
                        source_name,
                        title,
                        link,
                        meta["symbol_tag"],
                        meta["sentiment"],
                        pub_date,
                        now_iso,
                    ),
                )
        except Exception:
            continue

    conn.commit()
    cur.execute("SELECT * FROM news_items ORDER BY id DESC LIMIT ?", (max_items,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_recent_news_for_symbol(symbol: str, limit: int = 5) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM news_items
        WHERE symbol_tag = ? OR symbol_tag = 'MACRO/CRYPTO'
        ORDER BY id DESC LIMIT ?
        """,
        (symbol, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows
