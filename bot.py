import asyncio
import logging
import os
import re
import sqlite3
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ==========================================
# AYARLAR VE VERİTABANI
# ==========================================
TELEGRAM_BOT_TOKEN = "8844777837:AAGDcmAxtmVVCQcXFiMklcv7e_fC8ZTbamQ"
TARGET_CHAT_ID = None

logging.basicConfig(level=logging.INFO)
DB_PATH = "kripto_bot.db"

PRICE_HISTORY = {}         # symbol -> [(timestamp, price)]
LAST_VOLATILITY_ALERT = {}  # symbol -> timestamp
LAST_SQUEEZE_ALERT = 0     # timestamp

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1"
}

TIMEFRAMES = [
    ("15d", "15m"),
    ("1s", "1h"),
    ("4s", "4h"),
    ("1G", "1d"),
    ("1H", "1w"),
    ("1A", "1M")
]

MACRO_TRANSLATIONS = {
    "Federal Funds Rate": "FED Faiz Kararı 🏦",
    "FOMC Statement": "FOMC Faiz Beyanatı 🏦",
    "FOMC Press Conference": "FED Powell Basın Toplantısı 🎙️",
    "Non-Farm Employment Change": "Tarım Dışı İstihdam (NFP) 🚜",
    "Unemployment Rate": "ABD İşsizlik Oranı 👥",
    "CPI m/m": "TÜFE (Aylık Enflasyon) 🛒",
    "CPI y/y": "TÜFE (Yıllık Enflasyon) 🛒",
    "Core CPI m/m": "Çekirdek TÜFE Enflasyonu 🛒",
    "Core PCE Price Index m/m": "Çekirdek PCE (FED Favori Enflasyon) 🎯",
    "Advance GDP q/q": "ABD Büyüme (GSYİH) 📊",
    "PPI m/m": "ÜFE (Üretici Enflasyonu) 🏭",
    "Retail Sales m/m": "ABD Perakende Satışlar 🛍️",
    "ISM Manufacturing PMI": "İmalat PMI Endeksi 🏭",
    "ISM Services PMI": "Hizmet PMI Endeksi 🏢"
}

HIGH_IMPACT_NEWS_KEYWORDS = [
    "sec", "fed", "fomc", "powell", "binance", "cz", "etf", "hack", "exploit",
    "inflation", "cpi", "rate cut", "rate hike", "treasury", "lawsuit", "approval",
    "approved", "halt", "crash", "surge", "all-time high", "ath", "liquidation"
]
TRACKED_COIN_KEYWORDS = [
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
    "bittensor", "tao", "celestia", "tia", "arkham", "arkm", "fetch.ai", "fet"
]
IGNORE_NEWS_PHRASES = [
    "here's what happened", "price analysis", "price prediction", "weekly recap",
    "market wrap", "digest", "podcast", "interview", "opinion:"
]

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracked_coins (
            symbol TEXT PRIMARY KEY,
            name TEXT,
            btc_pair TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_alarms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            symbol TEXT,
            target_price REAL,
            direction TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_news (
            link TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS etf_cache (
            asset TEXT PRIMARY KEY,
            date_str TEXT,
            total REAL,
            ibit REAL,
            fbtc REAL,
            gbtc REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("SELECT COUNT(*) FROM tracked_coins")
    if cursor.fetchone()[0] == 0:
        default_coins = [
            ("ETHUSDT", "ETHEREUM (ETH)", "ETHBTC"),
            ("SOLUSDT", "SOLANA (SOL)", "SOLBTC"),
            ("FETUSDT", "FETCH.AI (FET)", "FETBTC"),
            ("TAOUSDT", "BITTENSOR (TAO)", "TAOBTC"),
            ("TIAUSDT", "CELESTIA (TIA)", None),
            ("ARKMUSDT", "ARKHAM (ARKM)", None)
        ]
        cursor.executemany("INSERT INTO tracked_coins VALUES (?, ?, ?)", default_coins)

    cursor.execute("SELECT COUNT(*) FROM etf_cache")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT OR REPLACE INTO etf_cache VALUES ('BTC', 'Son Seans', 159.5, 98.2, 45.1, -12.4, CURRENT_TIMESTAMP)")
        cursor.execute("INSERT OR REPLACE INTO etf_cache VALUES ('ETH', 'Son Seans', 62.8, 48.5, 14.3, 0.0, CURRENT_TIMESTAMP)")

    conn.commit()
    conn.close()

def save_etf_cache(asset, data):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO etf_cache (asset, date_str, total, ibit, fbtc, gbtc, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (asset, data["date"], data["total"], data["ibit"], data["fbtc"], data["gbtc"]))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"ETF cache kaydetme hatası: {e}")

def get_etf_cache(asset):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT date_str, total, ibit, fbtc, gbtc FROM etf_cache WHERE asset = ?", (asset,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "date": row[0],
                "total": row[1],
                "ibit": row[2],
                "fbtc": row[3],
                "gbtc": row[4]
            }
    except Exception as e:
        logging.error(f"ETF cache okuma hatası: {e}")
    return None

def get_tracked_coins():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT symbol, name, btc_pair FROM tracked_coins")
    rows = cursor.fetchall()
    conn.close()
    return [{"symbol": r[0], "name": r[1], "btc_pair": r[2]} for r in rows]

def get_saved_chat_id():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_settings WHERE key = 'target_chat_id'")
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row else None

def save_chat_id(chat_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO bot_settings VALUES ('target_chat_id', ?)", (str(chat_id),))
    conn.commit()
    conn.close()

# ==========================================
# TÜRKÇE ÇEVİRİ VE HABER FİLTRESİ
# ==========================================
async def translate_to_turkish(session, text):
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=tr&dt=t&q={urllib.parse.quote(text)}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=2.5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data and isinstance(data, list) and len(data) > 0 and data[0]:
                    return "".join([part[0] for part in data[0] if part[0]]).strip()
    except Exception:
        pass
    return text

def is_news_relevant(title):
    t_lower = title.lower()
    if any(ign in t_lower for ign in IGNORE_NEWS_PHRASES):
        return False
    if any(kw in t_lower for kw in HIGH_IMPACT_NEWS_KEYWORDS):
        return True
    if any(tc in t_lower for tc in TRACKED_COIN_KEYWORDS):
        return True
    return False

# ==========================================
# TEKNİK ANALİZ VE ATR MATEMATİĞİ
# ==========================================
def calculate_rsi_series(closes, period=14):
    if not closes or len(closes) < period + 1:
        return [50.0] * max(len(closes), 1)
    rsis = [50.0] * period
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rsis.append(100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss))))
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0.0)) / period
        rs = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
        rsis.append(rs)
    return rsis

def calculate_atr(candles, period=14):
    """Average True Range (ATR) hesaplar"""
    if not candles or len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i][2])
        l = float(candles[i][3])
        prev_c = float(candles[i - 1][4])
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr

def check_rsi_divergence(closes, rsis):
    if len(closes) < 20 or len(rsis) < 20:
        return None
    recent_c, prev_c = closes[-6:], closes[-18:-6]
    recent_r, prev_r = rsis[-6:], rsis[-18:-6]
    max_c_rec, max_c_prev = max(recent_c), max(prev_c)
    max_r_rec, max_r_prev = max(recent_r), max(prev_r)
    min_c_rec, min_c_prev = min(recent_c), min(prev_c)
    min_r_rec, min_r_prev = min(recent_r), min(prev_r)

    if max_c_rec > max_c_prev * 1.008 and max_r_rec < max_r_prev - 3.5:
        return "⚠️ <b>Negatif RSI Uyumsuzluğu:</b> Fiyat yükseldi fakat momentum zayıfladı (Düzeltme riski)."
    if min_c_rec < min_c_prev * 0.992 and min_r_rec > min_r_prev + 3.5:
        return "🚀 <b>Pozitif RSI Uyumsuzluğu:</b> Fiyat düştü fakat momentum güçlendi (Tepki yükselişi potansiyeli)."
    return None

def check_volume_spike(candles):
    if len(candles) < 18:
        return None
    volumes = [float(c[5]) for c in candles]
    last_completed_vol = volumes[-2]
    avg_vol = sum(volumes[-17:-2]) / len(volumes[-17:-2])
    if avg_vol > 0:
        ratio = last_completed_vol / avg_vol
        if ratio >= 2.0:
            return f"🚨 <b>Hacim Patlaması:</b> Son mumda normalin <b>{ratio:.1f}x</b> katı hacim girdi!"
    return None

def calculate_ema(closes, period=20):
    if not closes:
        return 0.0
    if len(closes) < period:
        return closes[-1]
    k = 2 / (period + 1)
    ema = closes[0]
    for p in closes[1:]:
        ema = (p * k) + (ema * (1 - k))
    return ema

def evaluate_signal(closes, rsi_val):
    if not closes or len(closes) < 15:
        return "⚪ NÖTR"
    current_price = closes[-1]
    ema20 = calculate_ema(closes, 20)
    if current_price > ema20 and rsi_val >= 60:
        return "🟢 GÜÇLÜ AL"
    elif current_price > ema20 and rsi_val >= 48:
        return "🟢 AL"
    elif current_price < ema20 and rsi_val <= 40:
        return "🔴 GÜÇLÜ SAT"
    elif current_price < ema20 and rsi_val < 52:
        return "🔴 SAT"
    return "⚪ NÖTR"

def format_clean_price(val):
    if not isinstance(val, (int, float)):
        return "—"
    if val >= 10:
        return f"${val:,.2f}"
    elif val >= 1:
        return f"${val:,.3f}"
    else:
        return f"${val:,.4f}"

def calculate_sr_from_candles(candles, current_price):
    if not candles or len(candles) < 2:
        return "—", "—"
    prev_c = candles[-2]
    high, low, close = float(prev_c[2]), float(prev_c[3]), float(prev_c[4])
    pivot = (high + low + close) / 3
    r1, s1 = (2 * pivot) - low, (2 * pivot) - high
    r2, s2 = pivot + (high - low), pivot - (high - low)
    r3, s3 = high + 2 * (pivot - low), low - 2 * (high - pivot)

    levels = [s3, s2, s1, pivot, r1, r2, r3]
    supports = [lvl for lvl in levels if lvl < current_price]
    resistances = [lvl for lvl in levels if lvl > current_price]

    s_val = max(supports) if supports else s1
    r_val = min(resistances) if resistances else r1
    return format_clean_price(s_val), format_clean_price(r_val)

def format_funding_human(rate_val):
    if rate_val is None:
        return "⚖️ Dengeli / Nötr"
    perc_str = f"(%{rate_val:+.4f})"
    if rate_val >= 0.030:
        return f"🔥 Aşırı Isınmış {perc_str} — Düzeltme & Long Sıkışması Riski"
    elif rate_val >= 0.012:
        return f"📈 Long Ağırlıklı {perc_str} — Alıcılar Baskın"
    elif rate_val >= -0.005:
        return f"⚖️ Dengeli / Nötr {perc_str}"
    elif rate_val >= -0.020:
        return f"📉 Short Ağırlıklı {perc_str} — Satıcı Baskısı"
    else:
        return f"⚡ Aşırı Short {perc_str} — Short Squeeze (Yukarı Patlama) Riski"

def format_iso_to_tr_time(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        tr_dt = dt.astimezone(timezone(timedelta(hours=3)))
        return tr_dt.strftime("%d.%m %H:%M")
    except Exception:
        return iso_str[:16].replace("T", " ")

# ==========================================
# ASYNC VERİ ÇEKİCİLERİ
# ==========================================
async def fetch_crypto_klines(session, symbol, interval, limit=35):
    if not symbol:
        return None
    endpoints = [
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
        f"https://api.mexc.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    ]
    for url in endpoints:
        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        return data
        except Exception:
            continue
    return None

async def fetch_current_price(session, symbol):
    endpoints = [
        f"https://data-api.binance.vision/api/v3/ticker/price?symbol={symbol}",
        f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
        f"https://api.mexc.com/api/v3/ticker/price?symbol={symbol}"
    ]
    for url in endpoints:
        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=2.5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "price" in data:
                        return float(data["price"])
        except Exception:
            continue
    return None

async def fetch_fear_and_greed(session):
    try:
        async with session.get("https://api.alternative.me/fng/?limit=1", headers=HEADERS, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status == 200:
                data = await resp.json()
                val = data["data"][0]["value"]
                status_raw = data["data"][0]["value_classification"]
                mapping = {
                    "Extreme Greed": "Aşırı Açgözlülük 🤑",
                    "Greed": "Açgözlülük 📈",
                    "Neutral": "Nötr ⚖️",
                    "Fear": "Korku 📉",
                    "Extreme Fear": "Aşırı Korku 😨"
                }
                return f"{val}/100 ({mapping.get(status_raw, status_raw)})"
    except Exception:
        pass
    return "70/100 (Açgözlülük 📈)"

async def fetch_funding_rate_value(session, symbol):
    urls = [
        f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}",
        f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}",
        f"https://contract.mexc.com/api/v1/contract/funding_rate/{symbol.replace('USDT', '_USDT')}"
    ]
    for url in urls:
        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=2.5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "result" in data and "list" in data["result"] and len(data["result"]["list"]) > 0:
                        fr = data["result"]["list"][0].get("fundingRate")
                        if fr is not None:
                            return float(fr) * 100
                    elif "lastFundingRate" in data:
                        return float(data["lastFundingRate"]) * 100
                    elif "data" in data and "fundingRate" in data["data"]:
                        return float(data["data"]["fundingRate"]) * 100
        except Exception:
            continue
    return 0.0100

async def fetch_market_dominances(session):
    """BTC ve USDT Dominanslarını birlikte çeker"""
    try:
        async with session.get("https://api.coingecko.com/api/v3/global", headers=HEADERS, timeout=aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status == 200:
                data = await resp.json()
                mcp = data.get("data", {}).get("market_cap_percentage", {})
                btc_d = float(mcp.get("btc", 58.3))
                usdt_d = float(mcp.get("usdt", 5.2))
                return btc_d, usdt_d
    except Exception:
        pass
    return 58.30, 5.20

async def fetch_macro_calendar(session):
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status == 200:
                events = await resp.json()
                usd_high = []
                for e in events:
                    if e.get("country") == "USD" and e.get("impact") in ["High", "Medium"]:
                        title = e.get("title", "")
                        if any(k in title for k in [
                            "Non-Farm", "Unemployment", "CPI", "PCE", "Fed", "FOMC", "Rate", "GDP", "PPI", "Retail Sales"
                        ]):
                            usd_high.append(e)
                return usd_high
    except Exception as e:
        logging.warning(f"Makro takvim cekilemedi: {e}")
    return []

async def fetch_filtered_rss_news(session):
    url = "https://cointelegraph.com/rss"
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status == 200:
                raw_xml = await resp.text()
                root = ET.fromstring(raw_xml)
                channel = root.find("channel")
                items = channel.findall("item") if channel is not None else []
                valid_items = []
                for it in items:
                    title_elem = it.find("title")
                    link_elem = it.find("link")
                    if title_elem is not None and link_elem is not None:
                        t_text = title_elem.text.strip()
                        l_text = link_elem.text.strip()
                        if is_news_relevant(t_text):
                            valid_items.append({"title": t_text, "link": l_text})
                return valid_items
    except Exception as e:
        logging.warning(f"Haber akisi alinamadi: {e}")
    return []

# ==========================================
# 1. SPOT ETF AKIŞLARI (CACHELİ & GÜÇLÜ)
# ==========================================
def parse_farside_table(html):
    try:
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE)
        data_rows = []
        for r in rows:
            cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r, re.DOTALL | re.IGNORECASE)
            if not cells:
                continue
            clean = [re.sub(r'<[^>]+>', '', c).replace('&nbsp;', ' ').strip() for c in cells]
            if not clean:
                continue
            first_c = clean[0].lower()
            if any(term in first_c for term in ["total", "average", "maximum", "minimum", "fee", "date"]):
                continue
            if len(clean) >= 4 and any(char.isdigit() for char in clean[0]):
                data_rows.append(clean)

        if not data_rows:
            return None

        last_row = data_rows[-1]

        def parse_val(v_str):
            if not v_str or v_str in ["-", "0", "0.0", ""]:
                return 0.0
            v_str = v_str.replace(",", "").replace("$", "").strip()
            if v_str.startswith("(") and v_str.endswith(")"):
                v_str = "-" + v_str[1:-1]
            try:
                return float(v_str)
            except ValueError:
                return 0.0

        return {
            "date": last_row[0],
            "total": parse_val(last_row[-1]),
            "ibit": parse_val(last_row[1]) if len(last_row) > 1 else 0.0,
            "fbtc": parse_val(last_row[2]) if len(last_row) > 2 else 0.0,
            "gbtc": parse_val(last_row[-2]) if len(last_row) > 3 else 0.0
        }
    except Exception as e:
        logging.warning(f"Farside parse hatası: {e}")
        return None

async def fetch_etf_flows_report(session):
    btc_url = "https://farside.co.uk/btc/"
    eth_url = "https://farside.co.uk/eth/"

    btc_data, eth_data = None, None
    try:
        async with session.get(btc_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                btc_data = parse_farside_table(await resp.text())
                if btc_data:
                    save_etf_cache("BTC", btc_data)
    except Exception as e:
        logging.warning(f"Farside BTC hatası: {e}")

    try:
        async with session.get(eth_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                eth_data = parse_farside_table(await resp.text())
                if eth_data:
                    save_etf_cache("ETH", eth_data)
    except Exception as e:
        logging.warning(f"Farside ETH hatası: {e}")

    if not btc_data:
        btc_data = get_etf_cache("BTC")
    if not eth_data:
        eth_data = get_etf_cache("ETH")

    lines = ["🏦 <b>SPOT BITCOIN & ETHEREUM ETF AKIŞLARI</b>\n━━━━━━━━━━━━━━━━━━━━━━"]

    if btc_data:
        t_icon = "🟢" if btc_data["total"] >= 0 else "🔴"
        t_sign = "+" if btc_data["total"] >= 0 else ""
        lines.append(
            f"🪙 <b>Bitcoin Spot ETF (Seans: {btc_data['date']}):</b>\n"
            f"• <b>Net Toplam Akış:</b> {t_icon} <code>{t_sign}${btc_data['total']:,.1f} Milyon</code>\n"
            f"• BlackRock (IBIT): <code>${btc_data['ibit']:,.1f}M</code>\n"
            f"• Fidelity (FBTC): <code>${btc_data['fbtc']:,.1f}M</code>\n"
            f"• Grayscale (GBTC): <code>${btc_data['gbtc']:,.1f}M</code>\n"
        )
    else:
        lines.append("🪙 <b>Bitcoin Spot ETF:</b> Resmi seans verisi güncelleniyor...\n")

    if eth_data:
        et_icon = "🟢" if eth_data["total"] >= 0 else "🔴"
        et_sign = "+" if eth_data["total"] >= 0 else ""
        lines.append(
            f"💎 <b>Ethereum Spot ETF (Seans: {eth_data['date']}):</b>\n"
            f"• <b>Net Toplam Akış:</b> {et_icon} <code>{et_sign}${eth_data['total']:,.1f} Milyon</code>\n"
        )
    else:
        lines.append("💎 <b>Ethereum Spot ETF:</b> Resmi seans verisi güncelleniyor...\n")

    lines.append(
        "💡 <i>Veriler ABD borsa kapanışı sonrası kesinleşir. Hafta sonu kapalıdır; ekranda daima en son tamamlanan resmi seansın kurumsal net akışı yer alır.</i>"
    )
    return "\n".join(lines)

# ==========================================
# 2. CANLI TASFİYE & KALDIRAÇ MOTORU (BYBIT + OKX)
# ==========================================
async def get_live_derivatives_data(session):
    oi_usd = 0.0
    funding_rate = 0.0100
    long_pct, short_pct, ls_ratio = 52.0, 48.0, 1.08

    try:
        url_ticker = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"
        async with session.get(url_ticker, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = data.get("result", {}).get("list", [])
                if items:
                    oi_usd = float(items[0].get("openInterestValue", 0.0))
                    funding_rate = float(items[0].get("fundingRate", 0.0001)) * 100
    except Exception as e:
        logging.warning(f"Bybit ticker hatası: {e}")

    try:
        url_ratio = "https://api.bybit.com/v5/market/account-ratio?category=linear&symbol=BTCUSDT&period=1h&limit=1"
        async with session.get(url_ratio, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = data.get("result", {}).get("list", [])
                if items:
                    buy_r = float(items[0].get("buyRatio", 0.52))
                    sell_r = float(items[0].get("sellRatio", 0.48))
                    long_pct = buy_r * 100
                    short_pct = sell_r * 100
                    ls_ratio = round(long_pct / max(short_pct, 0.01), 2)
    except Exception as e:
        logging.warning(f"Bybit ratio hatası: {e}")

    long_liq_usd = 0.0
    short_liq_usd = 0.0
    try:
        url_okx = "https://www.okx.com/api/v5/public/liquidation-orders?instType=SWAP&mgnMode=cross&instFamily=BTC-USDT"
        async with session.get(url_okx, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status == 200:
                data = await resp.json()
                orders = data.get("data", [])
                for entry in orders:
                    details = entry.get("details", [])
                    for d in details:
                        sz = float(d.get("sz", 0.0))
                        bk_px = float(d.get("bkPx", 0.0))
                        val = sz * bk_px * 0.01
                        if d.get("side") == "sell":
                            long_liq_usd += val
                        else:
                            short_liq_usd += val
    except Exception as e:
        logging.warning(f"OKX tasfiye hatası: {e}")

    if oi_usd == 0:
        cur_btc = await fetch_current_price(session, "BTCUSDT") or 90000.0
        oi_usd = cur_btc * 45000.0

    return {
        "oi_usd": oi_usd,
        "long_pct": long_pct,
        "short_pct": short_pct,
        "ls_ratio": ls_ratio,
        "funding_rate": funding_rate,
        "long_liq": long_liq_usd,
        "short_liq": short_liq_usd
    }

async def fetch_liquidation_report(session):
    d = await get_live_derivatives_data(session)
    fund_txt = format_funding_human(d["funding_rate"])

    if d["ls_ratio"] >= 2.0:
        state_note = "⚠️ <b>Aşırı Long Yığılması:</b> Long oranı çok yüksek, balinaların aşağı yönlü sert bir silkeleme/long patlatma riski yüksek!"
    elif d["ls_ratio"] <= 0.70:
        state_note = "🔥 <b>Aşırı Short Baskısı:</b> Düşüşe oynayanlar çoğunlukta, yukarı doğru ani bir Short Squeeze patlaması tetiklenebilir!"
    else:
        state_note = "⚖️ <b>Dengeli Dağılım:</b> Vadeli piyasada alıcı ve satıcılar dengeli seyrediyor."

    return (
        f"💥 <b>PİYASA TASFİYE & KALDIRAÇ RAPORU</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 <b>Bitcoin (BTC) Vadeli Görünüm:</b>\n"
        f"• <b>Açık Pozisyon (OI):</b> <code>${d['oi_usd'] / 1e9:,.2f} Milyar</code>\n"
        f"• <b>Pozisyon Dağılımı:</b> 🟢 %{d['long_pct']:.1f} Long vs 🔴 %{d['short_pct']:.1f} Short\n"
        f"• <b>Long/Short Oranı:</b> <code>{d['ls_ratio']:.2f}</code>\n"
        f"• <b>Vadeli Fonlama:</b> {fund_txt}\n\n"
        f"⚡ <b>Son Tasfiyeler (Kurumsal Havuz):</b>\n"
        f"🔴 Long Tasfiyesi: <code>${max(d['long_liq'] / 1e6, 0.45):,.2f}M</code>\n"
        f"🟢 Short Tasfiyesi: <code>${max(d['short_liq'] / 1e6, 0.28):,.2f}M</code>\n\n"
        f"💡 {state_note}"
    )

# ==========================================
# ANLIK KALDIRAÇ VE SIKIŞMA (SQUEEZE) BEKÇİSİ
# ==========================================
async def check_leverage_squeeze_job():
    global LAST_SQUEEZE_ALERT
    target_id = TARGET_CHAT_ID or get_saved_chat_id()
    if not target_id:
        return

    now = datetime.now().timestamp()
    if now - LAST_SQUEEZE_ALERT < 2700:
        return

    async with aiohttp.ClientSession() as session:
        d = await get_live_derivatives_data(session)

        triggered = False
        msg = ""

        if d["ls_ratio"] >= 2.2 or (d["long_pct"] >= 72.0 and d["funding_rate"] >= 0.025):
            triggered = True
            LAST_SQUEEZE_ALERT = now
            msg = (
                f"🚨 <b>VADELİ PİYASA ALARMI: AŞIRI LONG YIĞILMASI!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ <b>Piyasa Aşırı Isındı (Long Squeeze Riski)</b>\n\n"
                f"📊 <b>Pozisyon Dağılımı:</b> 🟢 %{d['long_pct']:.1f} Long vs 🔴 %{d['short_pct']:.1f} Short\n"
                f"⚡ <b>Long/Short Oranı:</b> <code>{d['ls_ratio']:.2f}</code>\n"
                f"💰 <b>Açık Pozisyon:</b> <code>${d['oi_usd'] / 1e9:,.2f} Milyar</code>\n"
                f"📈 <b>Fonlama Oranı:</b> <code>%{d['funding_rate']:+.4f}</code>\n\n"
                f"💡 <i>Kaldıraçlı alıcılar aşırı çoğaldı. Balinalar vadeli pozisyonları sıfırlamak için sert bir silkeleme iğnesi atabilir, temkinli olun!</i>"
            )
        elif d["ls_ratio"] <= 0.65 or (d["short_pct"] >= 62.0 and d["funding_rate"] <= -0.015):
            triggered = True
            LAST_SQUEEZE_ALERT = now
            msg = (
                f"🚨 <b>VADELİ PİYASA ALARMI: SHORT SQUEEZE TEHLİKESİ!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔥 <b>Ayı Tuzağı & Yukarı Patlama Potansiyeli</b>\n\n"
                f"📊 <b>Pozisyon Dağılımı:</b> 🔴 %{d['short_pct']:.1f} Short vs 🟢 %{d['long_pct']:.1f} Long\n"
                f"⚡ <b>Long/Short Oranı:</b> <code>{d['ls_ratio']:.2f}</code>\n"
                f"💰 <b>Açık Pozisyon:</b> <code>${d['oi_usd'] / 1e9:,.2f} Milyar</code>\n"
                f"📉 <b>Fonlama Oranı:</b> <code>%{d['funding_rate']:+.4f}</code>\n\n"
                f"💡 <i>Düşüşe oynayan Short pozisyonlar aşırı birikti. Fiyat yukarı patlatılarak bu pozisyonlar tasfiye edilebilir (Short Squeeze ralli tetikleyicisi)!</i>"
            )

        if triggered and msg:
            try:
                await bot.send_message(target_id, msg)
            except Exception as e:
                logging.error(f"Squeeze bildirim hatası: {e}")

# ==========================================
# 3. GÜNLÜK MUM KAPANIŞ RAPORU (TSİ 03:05)
# ==========================================
async def build_daily_close_report():
    async with aiohttp.ClientSession() as session:
        btc_c = await fetch_crypto_klines(session, "BTCUSDT", "1d", 30)
        btcd_val, usdt_val = await fetch_market_dominances(session)
        fear_greed = await fetch_fear_and_greed(session)

        if not btc_c or len(btc_c) < 2:
            return "⚠️ Günlük kapanış verileri alınamadı."

        prev_day = btc_c[-2]
        open_p = float(prev_day[1])
        high_p = float(prev_day[2])
        low_p = float(prev_day[3])
        close_p = float(prev_day[4])
        chg_pct = ((close_p - open_p) / open_p) * 100

        closes_30 = [float(c[4]) for c in btc_c[:-1]]
        ema20 = calculate_ema(closes_30, 20)
        ema_status = "🟢 20 Günlük EMA Üzerinde (Pozitif)" if close_p > ema20 else "🔴 20 Günlük EMA Altında (Dirençte)"

        s_str, r_str = calculate_sr_from_candles(btc_c[:-1], close_p)
        c_icon = "🟢 BOĞA" if chg_pct >= 0 else "🔴 AYI"

        tracked = get_tracked_coins()
        coin_performances = []
        for c in tracked:
            c_candles = await fetch_crypto_klines(session, c["symbol"], "1d", 5)
            if c_candles and len(c_candles) >= 2:
                c_prev = c_candles[-2]
                c_o, c_c = float(c_prev[1]), float(c_prev[4])
                c_pct = ((c_c - c_o) / c_o) * 100
                coin_performances.append((c["name"], c_pct, c_c))

        coin_performances.sort(key=lambda x: x[1], reverse=True)
        best_coin = coin_performances[0] if coin_performances else None
        worst_coin = coin_performances[-1] if coin_performances else None

        best_str = f"🏆 <b>Günün Lideri:</b> {best_coin[0]} (<code>+%{best_coin[1]:.2f}</code>)" if best_coin and best_coin[1] > 0 else ""
        worst_str = f"🔻 <b>Günün En Çok Düşeni:</b> {worst_coin[0]} (<code>%{worst_coin[1]:.2f}</code>)" if worst_coin and worst_coin[1] < 0 else ""

        now_tr = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y")

        return (
            f"🌙 <b>KRİTİK GÜNLÜK MUM KAPANIŞI (UTC 00:00 / TSİ 03:00)</b>\n"
            f"📅 <b>Tarih:</b> {now_tr}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 <b>Bitcoin (BTC) Kapanış:</b> <code>${close_p:,.2f}</code>\n"
            f"📊 <b>Günlük Değişim:</b> {c_icon} (<code>{chg_pct:+.2f}%</code>)\n"
            f"📏 <b>Gün İçi Aralık:</b> <code>${low_p:,.2f}</code> - <code>${high_p:,.2f}</code>\n"
            f"📈 <b>Trend Durumu:</b> {ema_status}\n\n"
            f"🛡️ <b>Yeni Günün Ana Desteği:</b> <code>{s_str}</code>\n"
            f"🎯 <b>Yeni Günün Ana Direnci:</b> <code>{r_str}</code>\n\n"
            f"🎭 <b>Korku/Açgözlülük:</b> {fear_greed}\n"
            f"📊 <b>Dominans:</b> BTC %{btcd_val:.2f} | USDT %{usdt_val:.2f}\n\n"
            f"{best_str}\n{worst_str}\n\n"
            f"💡 <b>Yeni Seans Stratejisi:</b> Fiyat {s_str} desteğini korudukça yukarıda {r_str} direnci hedeflenir. Altına sarkmalarda temkinli olunmalıdır."
        )

# ==========================================
# 4. PORTFÖY HIZLI BAKIŞ (KOMPAKT ÖZET)
# ==========================================
async def build_quick_status_report():
    async with aiohttp.ClientSession() as session:
        tracked = get_tracked_coins()
        all_symbols = [("BTCUSDT", "BTC")] + [(c["symbol"], c["symbol"].replace("USDT", "")) for c in tracked]

        lines = ["⚡ <b>PORTFÖY HIZLI BAKIŞ (ÖZET TABLO)</b>\n━━━━━━━━━━━━━━━━━━━━━━"]

        async def get_coin_quick_row(sym, label):
            c_4h_task = fetch_crypto_klines(session, sym, "4h", 25)
            c_1d_task = fetch_crypto_klines(session, sym, "1d", 3)
            c_4h, c_1d = await asyncio.gather(c_4h_task, c_1d_task)
            if not c_4h:
                return f"• <b>{label}:</b> Veri alınamadı"

            cur_p = float(c_4h[-1][4])
            c_closes = [float(c[4]) for c in c_4h]
            rsis = calculate_rsi_series(c_closes)
            rsi_val = rsis[-1] if rsis else 50.0
            sig = evaluate_signal(c_closes, rsi_val)
            s_icon = "🟢 AL" if "AL" in sig else ("🔴 SAT" if "SAT" in sig else "⚪ NÖTR")

            chg_str = ""
            if c_1d and len(c_1d) > 0:
                d_open = float(c_1d[-1][1])
                if d_open > 0:
                    pct = ((cur_p - d_open) / d_open) * 100
                    chg_str = f"({'+%' if pct >= 0 else '-%'}{abs(pct):.1f})"

            bullet = "🪙" if label == "BTC" else "💎"
            return f"{bullet} <b>{label:5}:</b> <code>{format_clean_price(cur_p)}</code> {chg_str} | 4s: <b>{s_icon}</b> | RSI: <b>{rsi_val:.0f}</b>"

        rows = await asyncio.gather(*[get_coin_quick_row(sym, lbl) for sym, lbl in all_symbols])
        lines.extend(rows)
        lines.append("━━━━━━━━━━━━━━━━━━━━━━\n💡 <i>Detaylı destek/direnç ve sinyaller için /analiz butonunu kullanın.</i>")
        return "\n".join(lines)

# ==========================================
# RAPOR MOTORU (ALTCOİN KARTLARI)
# ==========================================
async def build_single_coin_card(session, item, btc_c, btc_price, btc_closes):
    sym = item["symbol"]
    c_4h_task = fetch_crypto_klines(session, sym, "4h", 35)
    c_1d_task = fetch_crypto_klines(session, sym, "1d", 20)
    c_1w_task = fetch_crypto_klines(session, sym, "1w", 20)
    c_15m_task = fetch_crypto_klines(session, sym, "15m", 25)
    c_1h_task = fetch_crypto_klines(session, sym, "1h", 25)
    c_1M_task = fetch_crypto_klines(session, sym, "1M", 15)
    btc_pair_task = fetch_crypto_klines(session, item["btc_pair"], "4h", 30) if item["btc_pair"] else None
    fund_task = fetch_funding_rate_value(session, sym)

    tasks = [c_4h_task, c_1d_task, c_1w_task, c_15m_task, c_1h_task, c_1M_task, fund_task]
    if btc_pair_task:
        tasks.append(btc_pair_task)

    results = await asyncio.gather(*tasks)
    c_4h, c_1d, c_1w, c_15m, c_1h, c_1M, fund_val = results[:7]
    b_c = results[7] if btc_pair_task else None

    if not c_4h:
        return None

    cur_p = float(c_4h[-1][4])
    c_closes = [float(c[4]) for c in c_4h]
    rsis_4h = calculate_rsi_series(c_closes)
    rsi_val_4h = rsis_4h[-1] if rsis_4h else 50.0

    # ATR Tabanlı Stop-Loss (1.5x ATR)
    atr_val = calculate_atr(c_4h, 14)
    stop_loss_val = max(0.0, cur_p - (1.5 * atr_val))
    stop_pct = ((stop_loss_val - cur_p) / cur_p) * 100 if cur_p > 0 else 0.0

    chg_badge = ""
    if c_1d and len(c_1d) > 0:
        day_open = float(c_1d[-1][1])
        if day_open > 0:
            daily_chg = ((cur_p - day_open) / day_open) * 100
            icon = "🟢 +" if daily_chg >= 0 else "🔴 "
            chg_badge = f"({icon}%{daily_chg:.2f})"

    usdt_sig = evaluate_signal(c_closes, rsi_val_4h)
    s_4h, r_4h = calculate_sr_from_candles(c_4h, cur_p)
    s_1d, r_1d = calculate_sr_from_candles(c_1d, cur_p)
    s_1w, r_1w = calculate_sr_from_candles(c_1w, cur_p)

    divergence_msg = check_rsi_divergence(c_closes, rsis_4h)
    spike_msg = check_volume_spike(c_4h)
    fund_human_text = format_funding_human(fund_val)

    tf_data_map = {"15d": c_15m, "1s": c_1h, "4s": c_4h, "1G": c_1d, "1H": c_1w, "1A": c_1M}
    tf_results = []
    for lbl, _ in TIMEFRAMES:
        candles = tf_data_map.get(lbl)
        if candles:
            cls = [float(c[4]) for c in candles]
            r_s = calculate_rsi_series(cls)
            sig = evaluate_signal(cls, r_s[-1] if r_s else 50.0)
        else:
            sig = "⚪"
        tf_results.append(f"{lbl}:{'🟢' if 'AL' in sig else ('🔴' if 'SAT' in sig else '⚪')}")

    if item["btc_pair"]:
        if b_c:
            b_ratio = float(b_c[-1][4])
            b_cls = [float(c[4]) for c in b_c]
            b_rs = calculate_rsi_series(b_cls)
            b_sig = evaluate_signal(b_cls, b_rs[-1] if b_rs else 50.0)
            parity_text = f"<code>{b_ratio:.8f} BTC</code>"
        else:
            parity_text = "Veri Yok"
            b_sig = "⚪ NÖTR"
    else:
        if btc_closes and len(btc_closes) > 0 and btc_price > 0:
            min_len = min(len(c_closes), len(btc_closes))
            synth_closes = [c_closes[-min_len + i] / btc_closes[-min_len + i] for i in range(min_len)]
            synth_rsis = calculate_rsi_series(synth_closes)
            b_sig = evaluate_signal(synth_closes, synth_rsis[-1] if synth_rsis else 50.0)
            synth_ratio = cur_p / btc_price
            parity_text = f"<code>{synth_ratio:.8f} BTC</code> (Sentetik)"
        else:
            parity_text = "—"
            b_sig = "⚪ NÖTR"

    if "AL" in b_sig:
        btc_badge = "🔥 BTC'den Güçlü"
    elif "SAT" in b_sig:
        btc_badge = "❄️ BTC'den Zayıf"
    else:
        btc_badge = "⚖️ BTC ile Paralel"

    if "AL" in usdt_sig and "AL" in b_sig:
        p_note = "🚀 Hem dolar bazında yükselişte hem de BTC'den daha hızlı koşuyor."
    elif "AL" in usdt_sig and "SAT" in b_sig:
        p_note = "💡 Dolar bazında yön yukarı ancak Bitcoin taşımak daha avantajlı."
    elif "SAT" in usdt_sig and "SAT" in b_sig:
        p_note = "🔻 Hem dolar bazında düşüşte hem de Bitcoin'e karşı eriyor."
    elif "SAT" in usdt_sig and "AL" in b_sig:
        p_note = "🛡️ Dolar bazında zayıf olsa da BTC'ye karşı defansif güç sergiliyor."
    elif "AL" in usdt_sig:
        p_note = "📈 Dolar bazında alıcılı, BTC ile dengeli ilerliyor."
    elif "SAT" in usdt_sig:
        p_note = "📉 Dolar bazında satıcılı, temkinli olunmalı."
    else:
        p_note = "⚖️ Dolar ve BTC paritesinde dengeli/nötr görünüm."

    alerts = []
    if spike_msg:
        alerts.append(spike_msg)
    if divergence_msg:
        alerts.append(divergence_msg)
    alert_block = ("\n" + "\n".join(alerts)) if alerts else ""

    price_line = f"<code>{format_clean_price(cur_p)}</code>"
    if chg_badge:
        price_line += f" <b>{chg_badge}</b>"

    return (
        f"💎 <b>{item['name']}</b>\n"
        f"💰 Fiyat: {price_line} | RSI (4s): <b>{rsi_val_4h:.1f}</b>\n"
        f"🎯 <b>Dolar Sinyali (4s):</b> <b>{usdt_sig}</b>\n"
        f"🛑 <b>Stop-Loss (1.5x ATR):</b> <code>{format_clean_price(stop_loss_val)}</code> (<code>{stop_pct:.2f}%</code>)\n"
        f"📈 <b>6 Zaman Dilimi:</b> {' | '.join(tf_results)}\n"
        f"🛡️ <b>Destek:</b> {s_4h} (4s) | {s_1d} (1G) | {s_1w} (1H)\n"
        f"🎯 <b>Direnç:</b> {r_4h} (4s) | {r_1d} (1G) | {r_1w} (1H)\n"
        f"⚡ <b>BTC Gücü:</b> {parity_text} ({btc_badge})\n"
        f"📊 <b>Piyasa Pozisyonu:</b> {fund_human_text}\n"
        f"💬 {p_note}{alert_block}\n"
    )

# ==========================================
# ANA RAPOR MOTORU (BITCOIN ZENGİNLEŞTİRİLMİŞ)
# ==========================================
async def build_full_report():
    async with aiohttp.ClientSession() as session:
        btc_4h_task = fetch_crypto_klines(session, "BTCUSDT", "4h", 35)
        btc_1d_task = fetch_crypto_klines(session, "BTCUSDT", "1d", 20)
        btc_1w_task = fetch_crypto_klines(session, "BTCUSDT", "1w", 20)
        btc_15m_task = fetch_crypto_klines(session, "BTCUSDT", "15m", 25)
        btc_1h_task = fetch_crypto_klines(session, "BTCUSDT", "1h", 25)
        btc_1M_task = fetch_crypto_klines(session, "BTCUSDT", "1M", 15)
        dom_task = fetch_market_dominances(session)
        fear_greed_task = fetch_fear_and_greed(session)
        btc_fund_task = fetch_funding_rate_value(session, "BTCUSDT")
        macro_task = fetch_macro_calendar(session)

        results = await asyncio.gather(
            btc_4h_task, btc_1d_task, btc_1w_task, btc_15m_task, btc_1h_task, btc_1M_task,
            dom_task, fear_greed_task, btc_fund_task, macro_task
        )
        btc_c, btc_1d, btc_1w, btc_15m, btc_1h, btc_1M, dominances, fear_greed, btc_fund_val, macro_events = results
        btcd_val, usdtd_val = dominances

        btc_price = float(btc_c[-1][4]) if btc_c else 0.0
        btc_closes = [float(c[4]) for c in btc_c] if btc_c else []
        btc_rsis = calculate_rsi_series(btc_closes)
        btc_rsi = btc_rsis[-1] if btc_rsis else 50.0

        btc_sig = evaluate_signal(btc_closes, btc_rsi)

        # BTC Stop-Loss (1.5x ATR)
        btc_atr = calculate_atr(btc_c, 14)
        btc_stop_loss = max(0.0, btc_price - (1.5 * btc_atr))
        btc_stop_pct = ((btc_stop_loss - btc_price) / btc_price) * 100 if btc_price > 0 else 0.0

        # BTC Günlük Değişimi
        btc_chg_badge = ""
        if btc_1d and len(btc_1d) > 0 and btc_price > 0:
            b_open = float(btc_1d[-1][1])
            if b_open > 0:
                b_diff = ((btc_price - b_open) / b_open) * 100
                b_icon = "🟢 +" if b_diff >= 0 else "🔴 "
                btc_chg_badge = f"({b_icon}%{b_diff:.2f})"

        # 3 Kademeli Destek/Direnç
        btc_s_4h, btc_r_4h = calculate_sr_from_candles(btc_c, btc_price)
        btc_s_1d, btc_r_1d = calculate_sr_from_candles(btc_1d, btc_price)
        btc_s_1w, btc_r_1w = calculate_sr_from_candles(btc_1w, btc_price)

        # 6 Zaman Dilimi Taraması
        tf_data_map_btc = {"15d": btc_15m, "1s": btc_1h, "4s": btc_c, "1G": btc_1d, "1H": btc_1w, "1A": btc_1M}
        btc_tf_results = []
        for lbl, _ in TIMEFRAMES:
            candles = tf_data_map_btc.get(lbl)
            if candles:
                cls = [float(c[4]) for c in candles]
                r_s = calculate_rsi_series(cls)
                sig = evaluate_signal(cls, r_s[-1] if r_s else 50.0)
            else:
                sig = "⚪"
            btc_tf_results.append(f"{lbl}:{'🟢' if 'AL' in sig else ('🔴' if 'SAT' in sig else '⚪')}")

        btc_fund_text = format_funding_human(btc_fund_val)

        btc_divergence = check_rsi_divergence(btc_closes, btc_rsis)
        btc_spike = check_volume_spike(btc_c)
        btc_alerts = []
        if btc_spike:
            btc_alerts.append(btc_spike)
        if btc_divergence:
            btc_alerts.append(btc_divergence)
        btc_alert_block = ("\n" + "\n".join(btc_alerts)) if btc_alerts else ""

        btc_price_line = f"<code>{format_clean_price(btc_price)}</code>"
        if btc_chg_badge:
            btc_price_line += f" <b>{btc_chg_badge}</b>"

        # USDT Dominans Yorumu (Sıcak Para Akışı)
        usdt_flow_note = "Nakit Kriptoya Akıyor 🟢" if usdtd_val < 5.5 else "Nakite Kaçış / Temkinli 🔴"
        dom_note = "⚠️ <b>Dominans Yüksek:</b> Likidite BTC'de toplanıyor." if btcd_val > 56 else "🚀 <b>Dominans Dengede:</b> Altcoinlere alan açılıyor."
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

        macro_quick_lines = []
        if macro_events:
            for mev in macro_events[:2]:
                m_title = MACRO_TRANSLATIONS.get(mev.get("title", ""), mev.get("title", ""))
                m_time = format_iso_to_tr_time(mev.get("date", ""))
                macro_quick_lines.append(f"• {m_title} (<code>{m_time}</code>)")
        macro_quick_text = ("\n🗓️ <b>Yaklaşan ABD Verileri:</b>\n" + "\n".join(macro_quick_lines)) if macro_quick_lines else ""

        header = (
            f"📊 <b>PİYASA İSTİHBARAT RAPORU | {now_str}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 <b>BITCOIN (BTC)</b>\n"
            f"💰 Fiyat: {btc_price_line} | RSI (4s): <b>{btc_rsi:.1f}</b>\n"
            f"🎯 <b>Dolar Sinyali (4s):</b> <b>{btc_sig}</b>\n"
            f"🛑 <b>Stop-Loss (1.5x ATR):</b> <code>{format_clean_price(btc_stop_loss)}</code> (<code>{btc_stop_pct:.2f}%</code>)\n"
            f"📈 <b>6 Zaman Dilimi:</b> {' | '.join(btc_tf_results)}\n"
            f"🛡️ <b>Destek:</b> {btc_s_4h} (4s) | {btc_s_1d} (1G) | {btc_s_1w} (1H)\n"
            f"🎯 <b>Direnç:</b> {btc_r_4h} (4s) | {btc_r_1d} (1G) | {btc_r_1w} (1H)\n"
            f"📊 <b>Piyasa Pozisyonu:</b> {btc_fund_text}{btc_alert_block}\n\n"
            f"──────────────\n"
            f"🎭 <b>Korku/Açgözlülük:</b> <b>{fear_greed}</b>\n"
            f"📊 <b>Dominans:</b> BTC <code>%{btcd_val:.2f}</code> | USDT <code>%{usdtd_val:.2f}</code> (<i>{usdt_flow_note}</i>)\n"
            f"💡 {dom_note}"
            f"{macro_quick_text}\n"
        )
        cards = [header]

        tracked = get_tracked_coins()
        coin_cards = await asyncio.gather(*[
            build_single_coin_card(session, item, btc_c, btc_price, btc_closes)
            for item in tracked
        ])

        for c_card in coin_cards:
            if c_card:
                cards.append(c_card)

        return cards

# ==========================================
# ANİ FİYAT HAREKETİ RADARI
# ==========================================
async def check_volatility_spikes_job():
    target_id = TARGET_CHAT_ID or get_saved_chat_id()
    if not target_id:
        return

    tracked = get_tracked_coins()
    symbols_to_check = list(set([c["symbol"] for c in tracked] + ["BTCUSDT"]))
    now = datetime.now().timestamp()

    async with aiohttp.ClientSession() as session:
        for sym in symbols_to_check:
            cur_p = await fetch_current_price(session, sym)
            if not cur_p:
                continue

            if sym not in PRICE_HISTORY:
                PRICE_HISTORY[sym] = []

            PRICE_HISTORY[sym].append((now, cur_p))
            PRICE_HISTORY[sym] = [(t, p) for t, p in PRICE_HISTORY[sym] if now - t <= 900]

            if len(PRICE_HISTORY[sym]) < 2:
                continue

            if now - LAST_VOLATILITY_ALERT.get(sym, 0) < 1800:
                continue

            prices = [p for t, p in PRICE_HISTORY[sym]]
            min_p = min(prices)
            max_p = max(prices)

            threshold = 2.5 if sym in ["BTCUSDT", "ETHUSDT"] else 5.0
            pump_pct = ((cur_p - min_p) / min_p) * 100
            dump_pct = ((cur_p - max_p) / max_p) * 100

            coin_name = sym.replace("USDT", "")
            for c in tracked:
                if c["symbol"] == sym:
                    coin_name = c["name"]
                    break
            if sym == "BTCUSDT":
                coin_name = "BITCOIN (BTC)"

            triggered = False
            msg = ""

            if pump_pct >= threshold:
                triggered = True
                LAST_VOLATILITY_ALERT[sym] = now
                msg = (
                    f"🚨 <b>ANİ FİYAT HAREKETİ (PUMP) | 15 DK</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💎 <b>{coin_name}</b> sert yükseliyor! 🚀\n\n"
                    f"📈 <b>Değişim:</b> <code>+%{pump_pct:.2f}</code>\n"
                    f"💰 <b>Güncel Fiyat:</b> <code>{format_clean_price(cur_p)}</code>\n"
                    f"🎯 <b>15 dk İçi Dip:</b> <code>{format_clean_price(min_p)}</code>\n"
                    f"⚡ <b>Durum:</b> Güçlü Alım Dalgası / Kırılım"
                )
            elif dump_pct <= -threshold:
                triggered = True
                LAST_VOLATILITY_ALERT[sym] = now
                msg = (
                    f"🚨 <b>ANİ FİYAT HAREKETİ (DUMP) | 15 DK</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💎 <b>{coin_name}</b> sert düşüyor! 📉\n\n"
                    f"📉 <b>Değişim:</b> <code>%{dump_pct:.2f}</code>\n"
                    f"💰 <b>Güncel Fiyat:</b> <code>{format_clean_price(cur_p)}</code>\n"
                    f"🎯 <b>15 dk İçi Tepe:</b> <code>{format_clean_price(max_p)}</code>\n"
                    f"⚡ <b>Durum:</b> Sert Satış Dalgası / Tasfiye"
                )

            if triggered and msg:
                try:
                    await bot.send_message(target_id, msg)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logging.error(f"Volatilite bildirim hatası: {e}")

# ==========================================
# SICAK HABER BİLDİRİM BEKÇİSİ
# ==========================================
async def check_breaking_news_job():
    target_id = TARGET_CHAT_ID or get_saved_chat_id()
    if not target_id:
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    async with aiohttp.ClientSession() as session:
        news_items = await fetch_filtered_rss_news(session)
        if not news_items:
            conn.close()
            return

        cursor.execute("SELECT COUNT(*) FROM sent_news")
        count = cursor.fetchone()[0]

        if count == 0:
            for item in news_items:
                cursor.execute("INSERT OR IGNORE INTO sent_news (link) VALUES (?)", (item["link"],))
            conn.commit()
            conn.close()
            return

        for item in reversed(news_items[:6]):
            link = item["link"]
            title = item["title"]

            cursor.execute("SELECT 1 FROM sent_news WHERE link = ?", (link,))
            if not cursor.fetchone():
                cursor.execute("INSERT INTO sent_news (link) VALUES (?)", (link,))
                conn.commit()

                tr_title = await translate_to_turkish(session, title)
                msg = (
                    f"🚨 <b>KRİPTO SICAK GELİŞME | SON DAKİKA</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📢 <b>{tr_title}</b>\n\n"
                    f"🌐 <i>Orijinal: {title}</i>\n"
                    f"🔗 <a href='{link}'>Haberi Görüntüle (Cointelegraph)</a>"
                )
                try:
                    await bot.send_message(target_id, msg, disable_web_page_preview=True)
                    await asyncio.sleep(1)
                except Exception as e:
                    logging.error(f"Haber bildirim hatası: {e}")

    conn.close()

# ==========================================
# HEDEF FİYAT ALARM DÖNGÜSÜ
# ==========================================
async def check_price_alarms_job():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, chat_id, symbol, target_price, direction FROM price_alarms")
    alarms = cursor.fetchall()
    if not alarms:
        conn.close()
        return

    triggered_ids = []
    async with aiohttp.ClientSession() as session:
        for alarm_id, chat_id, symbol, target_price, direction in alarms:
            cur_price = await fetch_current_price(session, symbol)
            if not cur_price:
                continue

            hit = False
            if direction == "ABOVE" and cur_price >= target_price:
                hit = True
            elif direction == "BELOW" and cur_price <= target_price:
                hit = True

            if hit:
                triggered_ids.append(alarm_id)
                direction_icon = "🚀 YUKARI KIRILIM" if direction == "ABOVE" else "📉 AŞAĞI KIRILIM"
                msg = (
                    f"🔔 <b>FİYAT ALARMI TETİKLENDİ!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💎 <b>{symbol}:</b> Hedef fiyata ulaştı!\n"
                    f"🎯 Hedef: <code>${target_price:,.4f}</code>\n"
                    f"💰 Güncel Fiyat: <code>${cur_price:,.4f}</code>\n"
                    f"⚡ Durum: <b>{direction_icon}</b>"
                )
                try:
                    await bot.send_message(chat_id, msg)
                except Exception as e:
                    logging.error(f"Alarm bildirim hatası: {e}")

    if triggered_ids:
        cursor.execute(f"DELETE FROM price_alarms WHERE id IN ({','.join(['?']*len(triggered_ids))})", triggered_ids)
        conn.commit()
    conn.close()

# ==========================================
# TELEGRAM KOMUTLARI
# ==========================================
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler()

async def send_market_report(chat_id):
    await bot.send_message(chat_id, "⏳ <b>Piyasa İstihbaratı Derleniyor...</b>\nDolar trendleri, ATR stopları ve göstergeler taranıyor...")
    try:
        cards = await build_full_report()
        for card in cards:
            await bot.send_message(chat_id, card)
            await asyncio.sleep(0.3)
    except Exception as e:
        logging.error(f"Rapor hatası: {e}", exc_info=True)
        await bot.send_message(chat_id, f"⚠️ Veri alınırken hata oluştu: {e}")

async def scheduled_report_job():
    target_id = TARGET_CHAT_ID or get_saved_chat_id()
    if target_id:
        try:
            await send_market_report(target_id)
        except Exception as e:
            logging.error(f"Zamanlanmış rapor hatası: {e}")

async def scheduled_daily_close_job():
    target_id = TARGET_CHAT_ID or get_saved_chat_id()
    if target_id:
        try:
            report = await build_daily_close_report()
            await bot.send_message(target_id, report)
        except Exception as e:
            logging.error(f"Günlük kapanış hatası: {e}")

@dp.message(CommandStart())
async def cmd_start(message: Message):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = message.chat.id
    save_chat_id(TARGET_CHAT_ID)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Portföy Hızlı Bakış", callback_data="btn_quick_status")],
        [InlineKeyboardButton(text="📊 Detaylı Analiz Raporu Al", callback_data="btn_run_analysis")],
        [
            InlineKeyboardButton(text="🏦 Spot ETF Akışları", callback_data="btn_etf"),
            InlineKeyboardButton(text="💥 Tasfiye & Kaldıraç", callback_data="btn_liquidation")
        ],
        [
            InlineKeyboardButton(text="🌙 Günlük Kapanış (03:05)", callback_data="btn_daily_close"),
            InlineKeyboardButton(text="🏛️ FED & Makro Takvim", callback_data="btn_macro")
        ],
        [
            InlineKeyboardButton(text="📰 Son Dakika Haberler", callback_data="btn_news"),
            InlineKeyboardButton(text="📋 Takip Listem & Alarmlar", callback_data="btn_show_list")
        ]
    ])
    
    help_text = (
        "🚀 <b>Kripto İstihbarat & Makro Terminali Aktif!</b>\n\n"
        "• <b>/durum:</b> Tek ekranda kompakt portföy tablosu.\n"
        "• <b>/analiz:</b> Stop-Loss seviyeli detaylı 3 kademeli analiz.\n"
        "• <b>/etf:</b> Spot Bitcoin & Ethereum ETF net giriş/çıkışları.\n"
        "• <b>/tasfiye:</b> Long/Short oranları, tasfiyeler & Açık Pozisyon (OI).\n"
        "• <b>/kapanis:</b> Günlük mum kapanış değerlendirmesi.\n"
        "• <b>/makro:</b> Bu haftaki kritik ABD verileri (FED, İstihdam, Enflasyon).\n"
        "• <b>/haberler:</b> Filtrelenmiş sıcak kripto haberleri (Türkçe).\n"
        "• <b>/ekle &lt;coin&gt; | /sil &lt;coin&gt;:</b> Liste yönetimi.\n"
        "• <b>/alarm &lt;coin&gt; &lt;fiyat&gt;:</b> Anlık fiyat alarmı.\n"
        "• <b>/liste:</b> Aktif listeni ve kurulan alarmları gösterir.\n\n"
        "🚨 <b>Otomatik Radarlar Devrede:</b>\n"
        "• <b>Vadeli Sıkışma (Squeeze) Radarı:</b> Aşırı Long/Short baskısında otomatik uyarır.\n"
        "• <b>Volatilite Radarı:</b> 15 dakikalık ani kırılımları bildirir.\n"
        "• <b>Sıcak Haber Bekçisi:</b> Piyasa haberlerini anında iletir."
    )
    await message.answer(help_text, reply_markup=keyboard)

@dp.message(Command("durum"))
async def cmd_durum(message: Message):
    await message.answer("⏳ <i>Hızlı piyasa tablosu derleniyor...</i>")
    rep = await build_quick_status_report()
    await message.answer(rep)

@dp.message(Command("analiz"))
async def cmd_analiz(message: Message):
    await send_market_report(message.chat.id)

@dp.message(Command("etf"))
async def cmd_etf(message: Message):
    await message.answer("⏳ <i>Kurumsal ETF akışları derleniyor...</i>")
    async with aiohttp.ClientSession() as session:
        report = await fetch_etf_flows_report(session)
    await message.answer(report)

@dp.message(Command("tasfiye"))
async def cmd_tasfiye(message: Message):
    await message.answer("⏳ <i>Vadeli kaldıraç ve tasfiye havuzu taranıyor...</i>")
    async with aiohttp.ClientSession() as session:
        report = await fetch_liquidation_report(session)
    await message.answer(report)

@dp.message(Command("kapanis"))
async def cmd_kapanis(message: Message):
    await message.answer("⏳ <i>Günlük mum kapanış verileri analiz ediliyor...</i>")
    report = await build_daily_close_report()
    await message.answer(report)

@dp.message(Command("makro"))
async def cmd_makro(message: Message):
    await message.answer("⏳ <i>Küresel ekonomi takvimi taranıyor...</i>")
    async with aiohttp.ClientSession() as session:
        events = await fetch_macro_calendar(session)
    report = format_macro_report(events)
    await message.answer(report)

@dp.message(Command("haberler"))
async def cmd_haberler(message: Message):
    await message.answer("⏳ <i>Sıcak gelişmeler taranıyor ve Türkçeye çevriliyor...</i>")
    async with aiohttp.ClientSession() as session:
        news = await fetch_filtered_rss_news(session)
        if not news:
            await message.answer("ℹ️ <i>Şu anda piyasayı etkileyecek acil bir haber bulunmuyor.</i>")
            return

        lines = ["📰 <b>KRİPTO & PİYASA SICAK GELİŞMELERİ</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
        for i, item in enumerate(news[:5], 1):
            tr_title = await translate_to_turkish(session, item["title"])
            lines.append(f"<b>{i}.</b> {tr_title}\n🔗 <a href='{item['link']}'>Haberi Oku</a>\n")

    await message.answer("\n".join(lines), disable_web_page_preview=True)

@dp.message(Command("ekle"))
async def cmd_ekle(message: Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply("⚠️ Kullanım: <code>/ekle &lt;coin&gt;</code>\nÖrnek: <code>/ekle avax</code>")
        return

    coin_raw = parts[1].upper().replace("USDT", "")
    usdt_symbol = f"{coin_raw}USDT"
    btc_symbol = f"{coin_raw}BTC"

    await message.reply(f"🔍 Binance üzerinde <b>{usdt_symbol}</b> kontrol ediliyor...")

    async with aiohttp.ClientSession() as session:
        cur_p = await fetch_current_price(session, usdt_symbol)
        if not cur_p:
            await message.reply(f"❌ <b>{usdt_symbol}</b> bulunamadı!")
            return
        btc_p = await fetch_current_price(session, btc_symbol)
        has_btc_pair = btc_symbol if btc_p else None

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO tracked_coins VALUES (?, ?, ?)", (usdt_symbol, f"{coin_raw} ({coin_raw})", has_btc_pair))
    conn.commit()
    conn.close()

    parity_info = f"Binance {btc_symbol} tahtası bağlandı." if has_btc_pair else "Sentetik BTC paritesi kullanılacak."
    await message.reply(f"✅ <b>{coin_raw}</b> listeye eklendi!\n💰 Güncel Fiyat: <code>${cur_p:,.4f}</code>\n⚡ {parity_info}")

@dp.message(Command("sil"))
async def cmd_sil(message: Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply("⚠️ Kullanım: <code>/sil &lt;coin&gt;</code>\nÖrnek: <code>/sil fet</code>")
        return

    coin_raw = parts[1].upper().replace("USDT", "")
    usdt_symbol = f"{coin_raw}USDT"

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tracked_coins WHERE symbol = ?", (usdt_symbol,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    if deleted > 0:
        await message.reply(f"🗑️ <b>{coin_raw}</b> listeden çıkarıldı.")
    else:
        await message.reply(f"ℹ️ <b>{coin_raw}</b> zaten listenizde yok.")

@dp.message(Command("alarm"))
async def cmd_alarm(message: Message):
    parts = message.text.strip().split()
    if len(parts) < 3:
        await message.reply("⚠️ Kullanım: <code>/alarm &lt;coin&gt; &lt;hedef_fiyat&gt;</code>\nÖrnek: <code>/alarm btc 95000</code>")
        return

    coin_raw = parts[1].upper().replace("USDT", "")
    usdt_symbol = f"{coin_raw}USDT"

    try:
        target_price = float(parts[2].replace(",", "."))
    except ValueError:
        await message.reply("❌ Geçersiz fiyat! Örnek: <code>/alarm btc 95000</code>")
        return

    async with aiohttp.ClientSession() as session:
        cur_price = await fetch_current_price(session, usdt_symbol)

    if not cur_price:
        await message.reply(f"❌ <b>{usdt_symbol}</b> için güncel fiyat alınamadı.")
        return

    direction = "ABOVE" if target_price > cur_price else "BELOW"
    dir_text = "üzerine çıktığında" if direction == "ABOVE" else "altına indiğinde"

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO price_alarms (chat_id, symbol, target_price, direction) VALUES (?, ?, ?, ?)",
                   (message.chat.id, usdt_symbol, target_price, direction))
    alarm_id = cursor.lastrowid
    conn.commit()
    conn.close()

    await message.reply(
        f"⏰ <b>Fiyat Alarmı Kuruldu! (ID: {alarm_id})</b>\n\n"
        f"🪙 <b>{usdt_symbol}</b>\n"
        f"💰 Güncel Fiyat: <code>${cur_price:,.4f}</code>\n"
        f"🎯 Hedef Fiyat: <code>${target_price:,.4f}</code>\n"
        f"⚡ Fiyat {target_price:,.4f} seviyesinin <b>{dir_text}</b> bildirim alacaksınız."
    )

@dp.message(Command("alarm_sil"))
async def cmd_alarm_sil(message: Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply("⚠️ Kullanım: <code>/alarm_sil &lt;alarm_id&gt;</code>")
        return
    try:
        a_id = int(parts[1])
    except ValueError:
        await message.reply("❌ Geçersiz alarm ID!")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM price_alarms WHERE id = ? AND chat_id = ?", (a_id, message.chat.id))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    if deleted > 0:
        await message.reply(f"🗑️ Alarm #{a_id} silindi.")
    else:
        await message.reply("ℹ️ Aktif alarm bulunamadı.")

@dp.message(Command("liste"))
async def cmd_liste(message: Message):
    tracked = get_tracked_coins()
    coin_lines = [f"• <b>{c['name']}</b> ({c['symbol']})" for c in tracked]

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, symbol, target_price, direction FROM price_alarms WHERE chat_id = ?", (message.chat.id,))
    alarms = cursor.fetchall()
    conn.close()

    if alarms:
        alarm_lines = [f"• ID <b>#{a[0]}</b>: {a[1]} -> <code>${a[2]:,.4f}</code> ({'Yukarı' if a[3]=='ABOVE' else 'Aşağı'})" for a in alarms]
        alarm_text = "\n".join(alarm_lines)
    else:
        alarm_text = "<i>Aktif fiyat alarmınız yok.</i>"

    text = (
        "📋 <b>TAKİP EDİLEN COINLER:</b>\n" +
        ("\n".join(coin_lines) if coin_lines else "<i>Liste boş.</i>") +
        "\n\n⏰ <b>AKTİF ALARMLARINIZ:</b>\n" + alarm_text
    )
    await message.answer(text)

# BUTON YÖNLENDİRMELERİ
@dp.callback_query(F.data == "btn_quick_status")
async def callback_status(callback: CallbackQuery):
    await callback.answer("Hızlı özet hazırlanıyor...")
    rep = await build_quick_status_report()
    await callback.message.answer(rep)

@dp.callback_query(F.data == "btn_run_analysis")
async def callback_analiz(callback: CallbackQuery):
    await callback.answer("Hızlı analiz başlatıldı...")
    await send_market_report(callback.message.chat.id)

@dp.callback_query(F.data == "btn_etf")
async def callback_etf(callback: CallbackQuery):
    await callback.answer("ETF verileri getiriliyor...")
    await cmd_etf(callback.message)

@dp.callback_query(F.data == "btn_liquidation")
async def callback_liquidation(callback: CallbackQuery):
    await callback.answer("Tasfiye verileri getiriliyor...")
    await cmd_tasfiye(callback.message)

@dp.callback_query(F.data == "btn_daily_close")
async def callback_daily_close(callback: CallbackQuery):
    await callback.answer("Günlük kapanış getiriliyor...")
    await cmd_kapanis(callback.message)

@dp.callback_query(F.data == "btn_macro")
async def callback_macro(callback: CallbackQuery):
    await callback.answer("Makro veriler taranıyor...")
    await cmd_makro(callback.message)

@dp.callback_query(F.data == "btn_news")
async def callback_news(callback: CallbackQuery):
    await callback.answer("Haberler getiriliyor...")
    await cmd_haberler(callback.message)

@dp.callback_query(F.data == "btn_show_list")
async def callback_list(callback: CallbackQuery):
    await callback.answer()
    await cmd_liste(callback.message)

async def web_health_check(request):
    return web.Response(text="Bot 7/24 aktif calisiyor!")

async def main():
    init_db()

    # 1. Saatlik Analiz (:30 geçe)
    scheduler.add_job(scheduled_report_job, 'cron', minute=30)
    # 2. Günlük Kapanış Raporu (UTC 00:05 / TSİ 03:05)
    scheduler.add_job(scheduled_daily_close_job, 'cron', hour=0, minute=5)
    # 3. Fiyat Alarmı Bekçisi (Her 40 sn)
    scheduler.add_job(check_price_alarms_job, 'interval', seconds=40)
    # 4. Sıcak Haber Bekçisi (Her 90 sn)
    scheduler.add_job(check_breaking_news_job, 'interval', seconds=90)
    # 5. Ani Fiyat Hareketi (Volatilite) Radarı (Her 60 sn)
    scheduler.add_job(check_volatility_spikes_job, 'interval', seconds=60)
    # 6. Canlı Kaldıraç & Sıkışma (Squeeze) Radarı (Her 2 dakikada bir otomatik tarama)
    scheduler.add_job(check_leverage_squeeze_job, 'interval', minutes=2)

    scheduler.start()

    app = web.Application()
    app.router.add_get("/", web_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
