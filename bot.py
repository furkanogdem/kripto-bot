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
# AYARLAR VE VERİTABANI (SADECE HABER & MAKRO)
# ==========================================
TELEGRAM_BOT_TOKEN = "8844777837:AAGDcmAxtmVVCQcXFiMklcv7e_fC8ZTbamQ"
TARGET_CHAT_ID = None

logging.basicConfig(level=logging.INFO)
DB_PATH = "kripto_haber_makro.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*"
}

# Piyasayı sarsacak kritik anahtar kelimeler
CRITICAL_KEYWORDS = [
    "sec", "fed", "fomc", "powell", "etf", "blackrock", "fidelity", "grayscale",
    "hack", "exploit", "stolen", "lawsuit", "sues", "approval", "approved", "banned",
    "inflation", "cpi", "nfp", "rate cut", "rate hike", "treasury", "doj", "fbi",
    "arrest", "bankrupt", "chapter 11", "binance", "coinbase", "cz", "gary gensler"
]

RSS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "The Block": "https://www.theblock.co/rss.xml",
    "Decrypt": "https://decrypt.co/feed",
    "Blockworks": "https://blockworks.co/feed",
    "CoinTelegraph": "https://cointelegraph.com/rss",
    "Bitcoin Magazine": "https://bitcoinmagazine.com/feed"
}

MACRO_TRANSLATIONS = {
    "Federal Funds Rate": "FED Faiz Kararı 🏦",
    "FOMC Statement": "FOMC Faiz Beyanatı 🏦",
    "FOMC Press Conference": "FED Powell Basın Toplantısı 🎙",
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
        CREATE TABLE IF NOT EXISTS sent_news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link TEXT UNIQUE,
            title_en TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS macro_alerts (
            event_hash TEXT PRIMARY KEY,
            alert_1h INTEGER DEFAULT 0,
            alert_5m INTEGER DEFAULT 0,
            alert_result INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()

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
# ÇİFT MOTORLU TÜRKÇE ÇEVİRİ 
# ==========================================
async def translate_to_turkish(session, text):
    # 1. Yöntem: Google Translate API (Özel Tarayıcı Başlığıyla)
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=tr&dt=t&q={urllib.parse.quote(text)}"
        tr_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with session.get(url, headers=tr_headers, timeout=aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data and isinstance(data, list) and len(data) > 0 and data[0]:
                    return "".join([part[0] for part in data[0] if part[0]]).strip()
    except Exception as e:
        logging.warning(f"Google Translate hatası: {e}")

    # 2. Yöntem: MyMemory API (Google engellerse yedek devreye girer)
    try:
        url = f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(text)}&langpair=en|tr"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status == 200:
                data = await resp.json()
                translated = data.get("responseData", {}).get("translatedText", "")
                if translated and "MYMEMORY WARNING" not in translated:
                    return translated.strip()
    except Exception as e:
        logging.warning(f"MyMemory Translate hatası: {e}")

    # İkisi de tamamen çökerse İngilizce devam et
    return text

def is_similar_news(new_title, recent_titles):
    words_new = set(re.findall(r'\b\w{4,}\b', new_title.lower()))
    for old_title in recent_titles:
        words_old = set(re.findall(r'\b\w{4,}\b', old_title.lower()))
        if not words_new or not words_old:
            continue
        common = words_new.intersection(words_old)
        if len(common) >= 3:
            return True
    return False

# ==========================================
# ÇOKLU HABER AĞI MOTORU
# ==========================================
async def fetch_single_rss(session, source_name, url):
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                xml_data = await resp.text()
                root = ET.fromstring(xml_data)
                items = root.findall(".//item")
                parsed = []
                for it in items[:7]:
                    t_elem = it.find("title")
                    l_elem = it.find("link")
                    if t_elem is not None and l_elem is not None:
                        t_str = t_elem.text.strip()
                        l_str = l_elem.text.strip()
                        if any(kw in t_str.lower() for kw in CRITICAL_KEYWORDS):
                            parsed.append({"title": t_str, "link": l_str, "source": source_name})
                return parsed
    except Exception as e:
        logging.warning(f"{source_name} RSS hatası: {e}")
    return []

async def fetch_all_news_job():
    target_id = TARGET_CHAT_ID or get_saved_chat_id()
    if not target_id:
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT title_en FROM sent_news ORDER BY id DESC LIMIT 40")
    recent_titles = [r[0] for r in cursor.fetchall()]

    async with aiohttp.ClientSession() as session:
        tasks = [fetch_single_rss(session, name, url) for name, url in RSS_FEEDS.items()]
        results = await asyncio.gather(*tasks)
        
        all_news = []
        for r in results:
            all_news.extend(r)

        for news in all_news:
            link = news["link"]
            title_en = news["title"]
            source = news["source"]

            cursor.execute("SELECT 1 FROM sent_news WHERE link = ?", (link,))
            if cursor.fetchone():
                continue

            if is_similar_news(title_en, recent_titles):
                cursor.execute("INSERT INTO sent_news (link, title_en) VALUES (?, ?)", (link, title_en))
                conn.commit()
                continue

            cursor.execute("INSERT INTO sent_news (link, title_en) VALUES (?, ?)", (link, title_en))
            conn.commit()
            recent_titles.append(title_en)

            title_tr = await translate_to_turkish(session, title_en)

            msg = (
                f"🚨 <b>KRİPTO SICAK GELİŞME | {source.upper()}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📢 <b>{title_tr}</b>\n\n"
                f"🌐 <i>Orijinal: {title_en}</i>\n"
                f"🔗 <a href='{link}'>Haberi Görüntüle</a>"
            )
            try:
                await bot.send_message(target_id, msg, disable_web_page_preview=True)
                await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"Haber gönderim hatası: {e}")

    conn.close()

# ==========================================
# ABD MAKRO & FED RADARI
# ==========================================
def parse_macro_date(date_str):
    try:
        dt = datetime.fromisoformat(date_str)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

async def get_macro_events(session):
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                events = await resp.json()
                usd_high = []
                for e in events:
                    if e.get("country") == "USD" and e.get("impact") in ["High", "Medium"]:
                        t = e.get("title", "")
                        if any(k in t for k in ["CPI", "PCE", "Fed", "FOMC", "Non-Farm", "Unemployment", "GDP"]):
                            usd_high.append(e)
                return usd_high
    except Exception as e:
        logging.error(f"Makro çekim hatası: {e}")
    return []

async def macro_tracker_job():
    target_id = TARGET_CHAT_ID or get_saved_chat_id()
    if not target_id:
        return

    async with aiohttp.ClientSession() as session:
        events = await get_macro_events(session)
        if not events:
            return

        now_utc = datetime.now(timezone.utc)
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        for ev in events:
            title = ev.get("title", "")
            date_str = ev.get("date", "")
            actual = ev.get("actual", "")
            forecast = ev.get("forecast", "")
            previous = ev.get("previous", "")

            ev_dt = parse_macro_date(date_str)
            if not ev_dt:
                continue

            event_hash = f"{title}_{date_str[:10]}"
            cursor.execute("SELECT alert_1h, alert_5m, alert_result FROM macro_alerts WHERE event_hash = ?", (event_hash,))
            row = cursor.fetchone()

            if not row:
                cursor.execute("INSERT INTO macro_alerts (event_hash) VALUES (?)", (event_hash,))
                conn.commit()
                alert_1h, alert_5m, alert_result = 0, 0, 0
            else:
                alert_1h, alert_5m, alert_result = row

            time_diff = ev_dt - now_utc
            mins_left = time_diff.total_seconds() / 60.0

            tr_title = MACRO_TRANSLATIONS.get(title, title)
            event_time_tr = ev_dt.astimezone(timezone(timedelta(hours=3))).strftime("%H:%M")

            if 0 < mins_left <= 65 and alert_1h == 0:
                msg = (
                    f"⏳ <b>MAKRO VERİ UYARISI | 1 SAAT KALDI</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>{tr_title}</b>\n"
                    f"⏰ Açıklanma Saati: <code>{event_time_tr} (TSİ)</code>\n"
                    f"📊 Beklenti: <code>{forecast or 'Belirsiz'}</code> │ Önceki: <code>{previous or 'Belirsiz'}</code>\n\n"
                    f"<i>Piyasada volatilite (ani hareketlilik) başlayabilir, işlemlerinize dikkat edin!</i>"
                )
                await bot.send_message(target_id, msg)
                cursor.execute("UPDATE macro_alerts SET alert_1h = 1 WHERE event_hash = ?", (event_hash,))
                conn.commit()

            elif 0 < mins_left <= 6 and alert_5m == 0:
                msg = (
                    f"🚨 <b>KEMERLERİ BAĞLAYIN | SON 5 DAKİKA!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>{tr_title}</b> verisi 5 dakika içinde açıklanacak.\n"
                    f"⚡ <i>Algoritmik botlar devreye gireceğinden sert iğneler görülebilir.</i>"
                )
                await bot.send_message(target_id, msg)
                cursor.execute("UPDATE macro_alerts SET alert_5m = 1 WHERE event_hash = ?", (event_hash,))
                conn.commit()

            elif mins_left <= 0 and actual and alert_result == 0:
                comment = ""
                if "CPI" in title or "PCE" in title:
                    comment = "💡 <b>Yorum:</b> Enflasyon verisi açıklandı. (Düşük gelmesi FED'i rahatlatır, BTC için Boğa; Yüksek gelmesi Doları güçlendirir, BTC için Ayı algılanır.)"
                elif "Non-Farm" in title or "Unemployment" in title:
                    comment = "💡 <b>Yorum:</b> İstihdam verisi açıklandı. (İstihdamın düşük gelmesi piyasaya para basılacağı beklentisi yaratır -> BTC Pozitif.)"
                elif "Fed" in title or "FOMC" in title:
                    comment = "💡 <b>Yorum:</b> FED kararı geldi. Likidite ve faiz oranları tüm piyasanın kaderini belirleyecek."

                msg = (
                    f"💥 <b>ABD VERİSİ AÇIKLANDI!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>{tr_title}</b>\n\n"
                    f"🟢 <b>AÇIKLANAN:</b> <code>{actual}</code>\n"
                    f"🟡 <b>Beklenti :</b> <code>{forecast or 'Yok'}</code>\n"
                    f"⚪ <b>Önceki   :</b> <code>{previous or 'Yok'}</code>\n\n"
                    f"<blockquote>{comment}</blockquote>"
                )
                await bot.send_message(target_id, msg)
                cursor.execute("UPDATE macro_alerts SET alert_result = 1 WHERE event_hash = ?", (event_hash,))
                conn.commit()

        conn.close()

# ==========================================
# TELEGRAM KOMUTLARI
# ==========================================
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler()

@dp.message(CommandStart())
async def cmd_start(message: Message):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = message.chat.id
    save_chat_id(TARGET_CHAT_ID)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📰 Son Dakika Haberleri Tara", callback_data="btn_news")],
        [InlineKeyboardButton(text="🏛️ Bu Haftanın Makro Takvimi", callback_data="btn_macro")]
    ])
    
    help_text = (
        "🚀 <b>Küresel İstihbarat & Makro Terminali Aktif!</b>\n\n"
        "Fiyat analizleri tamamen kaldırıldı. Bu bot artık sadece piyasayı sarsacak <b>haberlere ve makroekonomik verilere</b> odaklıdır.\n\n"
        "📡 <b>Arka Plan Servisleri (Otomatik):</b>\n"
        "• <b>6 Kaynaklı Haber Ağı:</b> ABD ve Kripto medyasındaki kritik (SEC, ETF, Hack) gelişmeleri yakalar, Türkçeye çevirir ve anında iletir. (Tekrarlayan haberler elenir).\n"
        "• <b>Makro Geri Sayım:</b> Enflasyon, İstihdam ve FED kararlarına 1 saat kala, 5 dk kala uyarır; açıklandığı saniye sonucu ekrana basar.\n\n"
        "⚙️ <b>Manuel Komutlar:</b>\n"
        "• <b>/haberler :</b> Sistemi tetikleyip son manşetleri zorla çeker.\n"
        "• <b>/makro    :</b> Bu haftaki tüm kritik ABD takvimini listeler."
    )
    await message.answer(help_text, reply_markup=keyboard)

@dp.message(Command("makro"))
async def cmd_makro(message: Message):
    await message.answer("⏳ <i>Küresel ekonomi takvimi taranıyor...</i>")
    async with aiohttp.ClientSession() as session:
        events = await get_macro_events(session)
    
    if not events:
        await message.answer("📅 <i>Bu hafta için planlanan kritik bir ABD makro verisi bulunmuyor.</i>")
        return

    lines = ["🏦 <b>ABD MAKRO EKONOMİ TAKVİMİ (Bu Hafta)</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
    for ev in events:
        raw_title = ev.get("title", "")
        tr_name = MACRO_TRANSLATIONS.get(raw_title, raw_title)
        
        dt = parse_macro_date(ev.get("date", ""))
        time_str = dt.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M") if dt else ev.get("date", "")
        
        act, forc, prev = ev.get("actual"), ev.get("forecast"), ev.get("previous")
        
        if act:
            status = f"📊 Açıklanan: <code>{act}</code> │ Beklenti: {forc or '—'}"
        else:
            status = f"⏳ Beklenti: <code>{forc or '—'}</code> │ Önceki: <code>{prev or '—'}</code>"

        lines.append(f"📌 <b>{tr_name}</b>\n⏰ {time_str} TSİ\n{status}\n")

    await message.answer("\n".join(lines))

@dp.message(Command("haberler"))
async def cmd_haberler(message: Message):
    await message.answer("⏳ <i>Global haber ağı taranıyor... (Manuel Tetikleme)</i>")
    await fetch_all_news_job()
    await message.answer("✅ Tarama tamamlandı. Yeni kritik gelişme varsa iletildi.")

@dp.callback_query(F.data == "btn_news")
async def callback_news(callback: CallbackQuery):
    await callback.answer("Haber radarı tetiklendi...")
    await cmd_haberler(callback.message)

@dp.callback_query(F.data == "btn_macro")
async def callback_macro(callback: CallbackQuery):
    await callback.answer("Makro takvim getiriliyor...")
    await cmd_makro(callback.message)

async def web_health_check(request):
    return web.Response(text="İstihbarat Radarı 7/24 Aktif!")

async def main():
    init_db()

    # Otomatik Bekçiler (Çok Hızlı)
    # Her 60 saniyede bir haber ağlarını tarar
    scheduler.add_job(fetch_all_news_job, 'interval', seconds=60)
    # Her 60 saniyede bir Makro verileri tarar ve geri sayım kontrolü yapar
    scheduler.add_job(macro_tracker_job, 'interval', seconds=60)
    
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
