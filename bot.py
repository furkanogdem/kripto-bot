import asyncio
import logging
import os
import sqlite3
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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

TIMEFRAMES = [
    ("15d", "15m"),
    ("1s", "1h"),
    ("4s", "4h"),
    ("1G", "1d"),
    ("1H", "1w"),
    ("1A", "1M")
]

# ÖNEMLİ ABD MAKRO VERİLERİ ÇEVİRİ TABLOSU
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

    conn.commit()
    conn.close()

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
# TEKNİK ANALİZ MATEMATİĞİ
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
        return "⚠️ <b>Negatif RSI Uyumsuzluğu:</b> Fiyat yeni tepe yaptı fakat momentum zayıfladı (Düzeltme riski)."
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
    if len(closes) < period:
        return closes[-1] if closes else 0.0
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

def calculate_daily_sr(daily_candles, current_price):
    if not daily_candles or len(daily_candles) < 2:
        return "—", "—"
    prev_day = daily_candles[-2]
    high, low, close = float(prev_day[2]), float(prev_day[3]), float(prev_day[4])
    pivot = (high + low + close) / 3
    r1, s1 = (2 * pivot) - low, (2 * pivot) - high
    r2, s2 = pivot + (high - low), pivot - (high - low)
    r3, s3 = high + 2 * (pivot - low), low - 2 * (high - pivot)

    levels = [s3, s2, s1, pivot, r1, r2, r3]
    supports = [lvl for lvl in levels if lvl < current_price]
    resistances = [lvl for lvl in levels if lvl > current_price]
    s_val = max(supports) if supports else s1
    r_val = min(resistances) if resistances else r1
    return f"${s_val:,.4f}", f"${r_val:,.4f}"

def format_funding_human(rate_val):
    if rate_val is None:
        return "⚖️ Dengeli / Nötr"
    perc_str = f"(%{rate_val:+.4f})"
    if rate_val >= 0.035:
        return f"⚠️ Aşırı Long {perc_str} — Düzeltme Riski"
    elif rate_val > 0.015:
        return f"📈 Long Ağırlıklı {perc_str} — Alıcılar Baskın"
    elif rate_val >= 0.005:
        return f"⚖️ Dengeli / Nötr {perc_str}"
    elif rate_val > -0.010:
        return f"📉 Temkinli {perc_str} — Satıcı Eğilimli"
    elif rate_val >= -0.030:
        return f"🔻 Short Ağırlıklı {perc_str} — Düşüş Beklentisi"
    else:
        return f"🔥 Aşırı Short {perc_str} — Squeeze (Patlama) Riski"

def format_iso_to_tr_time(iso_str):
    """ISO formatındaki tarihi Türkiye saatine (UTC+3) çevirir"""
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
        f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}",
        f"https://contract.mexc.com/api/v1/contract/funding_rate/{symbol.replace('USDT', '_USDT')}"
    ]
    for url in urls:
        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=2.5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "lastFundingRate" in data:
                        return float(data["lastFundingRate"]) * 100
                    elif "data" in data and "fundingRate" in data["data"]:
                        return float(data["data"]["fundingRate"]) * 100
        except Exception:
            continue
    return None

async def fetch_btc_dominance(session):
    try:
        async with session.get("https://api.coingecko.com/api/v3/global", headers=HEADERS, timeout=aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("data", {}).get("market_cap_percentage", {}).get("btc", 0.0)
    except Exception:
        pass
    return 58.30

# ==========================================
# MAKRO EKONOMİK VERİ VE HABER MOTORU
# ==========================================
async def fetch_macro_calendar(session):
    """ForexFactory üzerinden haftalık kritik ABD verilerini çeker"""
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status == 200:
                events = await resp.json()
                usd_high = []
                for e in events:
                    if e.get("country") == "USD" and e.get("impact") in ["High", "Medium"]:
                        title = e.get("title", "")
                        # Özellikle BTC'yi vuran verileri seç
                        if any(k in title for k in [
                            "Non-Farm", "Unemployment", "CPI", "PCE", "Fed", "FOMC", "Rate", "GDP", "PPI", "Retail Sales"
                        ]):
                            usd_high.append(e)
                return usd_high
    except Exception as e:
        logging.warning(f"Makro takvim cekilemedi: {e}")
    return []

async def fetch_latest_crypto_news(session, limit=5):
    """CoinTelegraph RSS feed üzerinden son sıcak haberleri çeker"""
    url = "https://cointelegraph.com/rss"
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status == 200:
                raw_xml = await resp.text()
                root = ET.fromstring(raw_xml)
                channel = root.find("channel")
                items = channel.findall("item") if channel is not None else []
                news_list = []
                for it in items[:limit]:
                    title_elem = it.find("title")
                    link_elem = it.find("link")
                    if title_elem is not None and link_elem is not None:
                        news_list.append({
                            "title": title_elem.text.strip(),
                            "link": link_elem.text.strip()
                        })
                return news_list
    except Exception as e:
        logging.warning(f"Haber akisi alinamadi: {e}")
    return []

def format_macro_report(events):
    """Çekilen makro verileri şık bir mesaja dönüştürür"""
    if not events:
        return "📅 <i>Bu hafta için planlanan kritik bir ABD makro verisi bulunmuyor veya takvim henüz güncellenmedi.</i>"

    lines = []
    for ev in events[:6]:
        raw_title = ev.get("title", "")
        tr_name = MACRO_TRANSLATIONS.get(raw_title, raw_title)
        event_time = format_iso_to_tr_time(ev.get("date", ""))
        actual = ev.get("actual")
        forecast = ev.get("forecast")
        previous = ev.get("previous")

        status_line = ""
        if actual:
            status_line = f"📊 <b>Açıklanan:</b> <code>{actual}</code> | <b>Beklenti:</b> {forecast or '—'} | <b>Önceki:</b> {previous or '—'}"
        else:
            status_line = f"⏳ <b>Beklenti:</b> <code>{forecast or '—'}</code> | <b>Önceki:</b> <code>{previous or '—'}</code>"

        lines.append(
            f"📌 <b>{tr_name}</b>\n"
            f"⏰ <b>Tarih/Saat:</b> <code>{event_time} (TSİ)</code>\n"
            f"{status_line}\n"
        )

    cheat_sheet = (
        "💡 <b>Bitcoin & Kripto Etki Rehberi:</b>\n"
        "• <b>İstihdam (NFP) & Enflasyon (TÜFE/PCE) Düşük Gelirse:</b> FED faiz indirimine mecbur kalır $\rightarrow$ <b>BTC YÜKSELİR (Boğa) 🚀</b>\n"
        "• <b>İstihdam & Enflasyon Yüksek Gelirse:</b> Dolar (DXY) güçlenir $\rightarrow$ <b>BTC BASKILANIR 📉</b>\n"
        "• <b>FED Faiz İndirirse:</b> Küresel likidite artar $\rightarrow$ <b>Piyasa Rallisi Başlar 🔥</b>"
    )

    return "🏦 <b>ABD MAKRO EKONOMİ & FED TAKVİMİ (Bu Hafta)</b>\n━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines) + "\n" + cheat_sheet

# ==========================================
# RAPOR MOTORU
# ==========================================
async def build_single_coin_card(session, item, btc_c, btc_price, btc_closes):
    sym = item["symbol"]
    c_4h_task = fetch_crypto_klines(session, sym, "4h", 35)
    c_1d_task = fetch_crypto_klines(session, sym, "1d", 15)
    c_15m_task = fetch_crypto_klines(session, sym, "15m", 25)
    c_1h_task = fetch_crypto_klines(session, sym, "1h", 25)
    c_1w_task = fetch_crypto_klines(session, sym, "1w", 20)
    c_1M_task = fetch_crypto_klines(session, sym, "1M", 15)
    btc_pair_task = fetch_crypto_klines(session, item["btc_pair"], "4h", 30) if item["btc_pair"] else None
    fund_task = fetch_funding_rate_value(session, sym)

    tasks = [c_4h_task, c_1d_task, c_15m_task, c_1h_task, c_1w_task, c_1M_task, fund_task]
    if btc_pair_task:
        tasks.append(btc_pair_task)

    results = await asyncio.gather(*tasks)
    c_4h, c_1d, c_15m, c_1h, c_1w, c_1M, fund_val = results[:7]
    b_c = results[7] if btc_pair_task else None

    if not c_4h:
        return None

    cur_p = float(c_4h[-1][4])
    c_closes = [float(c[4]) for c in c_4h]
    rsis_4h = calculate_rsi_series(c_closes)
    rsi_val_4h = rsis_4h[-1] if rsis_4h else 50.0

    s_str, r_str = calculate_daily_sr(c_1d, cur_p)
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

    p_note = "🔥 BTC'den Güçlü" if "AL" in b_sig else ("❄️ BTC'den Zayıf" if "SAT" in b_sig else "⚖️ BTC ile Paralel")
    alerts = []
    if spike_msg:
        alerts.append(spike_msg)
    if divergence_msg:
        alerts.append(divergence_msg)
    alert_block = ("\n" + "\n".join(alerts)) if alerts else ""

    return (
        f"💎 <b>{item['name']}</b>\n"
        f"💰 Fiyat: <code>${cur_p:,.4f}</code> | RSI (4s): <b>{rsi_val_4h:.1f}</b>\n"
        f"📈 <b>6 Zaman Dilimi:</b> {' | '.join(tf_results)}\n"
        f"🛡️ Destek (Günlük): <code>{s_str}</code> | 🎯 Direnç: <code>{r_str}</code>\n"
        f"⚡ <b>BTC Paritesi:</b> {parity_text} ({b_sig})\n"
        f"📊 <b>Piyasa Pozisyonu:</b> {fund_human_text}\n"
        f"💬 {p_note}{alert_block}\n"
    )

async def build_full_report():
    async with aiohttp.ClientSession() as session:
        # Piyasa genel verileri ve Makro Takvimi paralel çek
        btc_c, btcd_val, fear_greed, btc_fund_val, macro_events = await asyncio.gather(
            fetch_crypto_klines(session, "BTCUSDT", "4h", 35),
            fetch_btc_dominance(session),
            fetch_fear_and_greed(session),
            fetch_funding_rate_value(session, "BTCUSDT"),
            fetch_macro_calendar(session)
        )

        btc_price = float(btc_c[-1][4]) if btc_c else 0.0
        btc_closes = [float(c[4]) for c in btc_c] if btc_c else []
        btc_rsis = calculate_rsi_series(btc_closes)
        btc_rsi = btc_rsis[-1] if btc_rsis else 50.0
        btc_sig = evaluate_signal(btc_closes, btc_rsi)
        btc_fund_text = format_funding_human(btc_fund_val)

        dom_note = "⚠️ <b>Dominans Yüksek:</b> Likidite BTC'de toplanıyor." if btcd_val > 56 else "🚀 <b>Dominans Dengede:</b> Altcoinlere alan açılıyor."
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

        # Kısa Makro Hatırlatma (En yakın 2 kritik veri)
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
            f"🪙 <b>Bitcoin (BTC):</b> <code>${btc_price:,.2f}</code> | RSI (4s): <b>{btc_rsi:.1f}</b>\n"
            f"🎯 Sinyal (4s): <b>{btc_sig}</b>\n"
            f"📈 <b>Piyasa Pozisyonu:</b> {btc_fund_text}\n\n"
            f"🎭 <b>Korku/Açgözlülük:</b> <b>{fear_greed}</b>\n"
            f"📊 <b>BTC Dominansı:</b> <code>%{btcd_val:.2f}</code>\n"
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
# ALARM DÖNGÜSÜ
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
    await bot.send_message(chat_id, "⏳ <b>Piyasa İstihbaratı Derleniyor...</b>\nTüm pariteler ve makro göstergeler taranıyor...")
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
            logging.error(f"Zamanlanmış bildirim hatası: {e}")

@dp.message(CommandStart())
async def cmd_start(message: Message):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = message.chat.id
    save_chat_id(TARGET_CHAT_ID)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Şimdi Analiz Raporu Al", callback_data="btn_run_analysis")],
        [
            InlineKeyboardButton(text="🏦 FED & Makro Veriler", callback_data="btn_macro"),
            InlineKeyboardButton(text="📰 Son Dakika Haberler", callback_data="btn_news")
        ],
        [InlineKeyboardButton(text="📋 Takip Listem & Alarmlar", callback_data="btn_show_list")]
    ])
    
    help_text = (
        "🚀 <b>Kripto İstihbarat & Makro Terminali Aktif!</b>\n\n"
        "• <b>/analiz:</b> Teknik göstergeler ve piyasa pozisyon raporu.\n"
        "• <b>/makro:</b> Bu haftaki kritik ABD verileri (FED, İstihdam, TÜFE).\n"
        "• <b>/haberler:</b> En güncel sıcak kripto ve makro haberler.\n"
        "• <b>/ekle &lt;coin&gt; | /sil &lt;coin&gt;:</b> Liste yönetimi.\n"
        "• <b>/alarm &lt;coin&gt; &lt;fiyat&gt;:</b> Anlık fiyat alarmı.\n"
        "• <b>/liste:</b> Aktif listeni ve kurulan alarmları gösterir.\n\n"
        "⏰ Her saatin <b>:30 geçesinde</b> otomatik rapor iletilecektir."
    )
    await message.answer(help_text, reply_markup=keyboard)

@dp.message(Command("analiz"))
async def cmd_analiz(message: Message):
    await send_market_report(message.chat.id)

@dp.message(Command("makro"))
async def cmd_makro(message: Message):
    await message.answer("⏳ <i>Küresel ekonomi takvimi taranıyor...</i>")
    async with aiohttp.ClientSession() as session:
        events = await fetch_macro_calendar(session)
    report = format_macro_report(events)
    await message.answer(report)

@dp.message(Command("haberler"))
async def cmd_haberler(message: Message):
    await message.answer("⏳ <i>Son dakika haber akışı taranıyor...</i>")
    async with aiohttp.ClientSession() as session:
        news = await fetch_latest_crypto_news(session, limit=5)

    if not news:
        await message.answer("ℹ️ <i>Şu anda yeni bir haber akışı çekilemedi.</i>")
        return

    lines = ["📰 <b>KRİPTO & PİYASA SON DAKİKA GELİŞMELERİ</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
    for i, item in enumerate(news, 1):
        lines.append(f"<b>{i}.</b> {item['title']}\n🔗 <a href='{item['link']}'>Haberi Görüntüle</a>\n")

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
@dp.callback_query(F.data == "btn_run_analysis")
async def callback_analiz(callback: CallbackQuery):
    await callback.answer("Hızlı analiz başlatıldı...")
    await send_market_report(callback.message.chat.id)

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

    scheduler.add_job(scheduled_report_job, 'cron', minute=30)
    scheduler.add_job(check_price_alarms_job, 'interval', seconds=40)
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
