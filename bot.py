import asyncio
import logging
import os
from datetime import datetime
import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler

TELEGRAM_BOT_TOKEN = "8844777837:AAGDcmAxtmVVCQcXFiMklcv7e_fC8ZTbamQ"
TARGET_CHAT_ID = None

logging.basicConfig(level=logging.INFO)

TRACKED_COINS = [
    {"name": "ETHEREUM (ETH)", "symbol": "ETHUSDT", "btc_pair": "ETHBTC"},
    {"name": "SOLANA (SOL)", "symbol": "SOLUSDT", "btc_pair": "SOLBTC"},
    {"name": "FETCH.AI (FET)", "symbol": "FETUSDT", "btc_pair": "FETBTC"},
    {"name": "BITTENSOR (TAO)", "symbol": "TAOUSDT", "btc_pair": "TAOBTC"},
    {"name": "CELESTIA (TIA)", "symbol": "TIAUSDT", "btc_pair": None},
    {"name": "ARKHAM (ARKM)", "symbol": "ARKMUSDT", "btc_pair": None}
]

TIMEFRAMES = [
    ("15d", "15m"),
    ("1s", "1h"),
    ("4s", "4h"),
    ("1G", "1d"),
    ("1H", "1w"),
    ("1A", "1M")
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def calculate_ema(closes, period=20):
    if len(closes) < period:
        return closes[-1]
    k = 2 / (period + 1)
    ema = closes[0]
    for p in closes[1:]:
        ema = (p * k) + (ema * (1 - k))
    return ema

def evaluate_signal(closes):
    if len(closes) < 15:
        return "⚪ NÖTR"
    current_price = closes[-1]
    ema20 = calculate_ema(closes, 20)
    rsi = calculate_rsi(closes, 14)
    if current_price > ema20 and rsi >= 60:
        return "🟢 GÜÇLÜ AL"
    elif current_price > ema20 and rsi >= 48:
        return "🟢 AL"
    elif current_price < ema20 and rsi <= 40:
        return "🔴 GÜÇLÜ SAT"
    elif current_price < ema20 and rsi < 52:
        return "🔴 SAT"
    return "⚪ NÖTR"

def calculate_dynamic_sr(candles, current_price):
    if len(candles) < 2:
        return "—", "—"
    prev = candles[-2]
    h, l, c = float(prev[2]), float(prev[3]), float(prev[4])
    p = (h + l + c) / 3
    r1, s1 = (2 * p) - l, (2 * p) - h
    r2, s2 = p + (h - l), p - (h - l)
    if current_price >= r1:
        return f"${r1:,.4f}", f"${r2:,.4f}"
    elif current_price <= s1:
        return f"${s2:,.4f}", f"${s1:,.4f}"
    return f"${s1:,.4f}", f"${r1:,.4f}"

# BULUT ENGELİNİ AŞAN ÇOKLU API İSTEMCİSİ
async def fetch_crypto_klines(session, symbol, interval, limit=30):
    endpoints = [
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
        f"https://api.mexc.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    ]
    for url in endpoints:
        try:
            async with session.get(url, headers=HEADERS, timeout=4) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        return data
        except Exception:
            continue
    return None

async def fetch_btc_dominance(session):
    try:
        async with session.get("https://api.coingecko.com/api/v3/global", headers=HEADERS, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("data", {}).get("market_cap_percentage", {}).get("btc", 0.0)
    except Exception:
        pass
    return 58.30

async def build_full_report():
    async with aiohttp.ClientSession() as session:
        btc_c = await fetch_crypto_klines(session, "BTCUSDT", "4h", 30)
        btc_price = float(btc_c[-1][4]) if btc_c else 0.0
        btc_closes = [float(c[4]) for c in btc_c] if btc_c else []
        btc_rsi = calculate_rsi(btc_closes)
        btc_sig = evaluate_signal(btc_closes)
        btcd_val = await fetch_btc_dominance(session)
        
        dom_note = "⚠️ <b>Dominans Yüksek:</b> Altcoinlerde baskı sürebilir." if btcd_val > 56 else "🚀 <b>Dominans Dengede:</b> Altcoinler için hareket alanı açık."
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        
        cards = [
            f"📊 <b>PİYASA ANALİZ RAPORU | {now_str}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 <b>Bitcoin (BTC):</b> <code>${btc_price:,.2f}</code> | RSI (4s): <b>{btc_rsi:.1f}</b>\n"
            f"🎯 BTC Sinyali (4s): <b>{btc_sig}</b>\n\n"
            f"📈 <b>BTC.D (Dominans):</b> <code>%{btcd_val:.2f}</code>\n"
            f"💡 {dom_note}\n"
        ]

        for item in TRACKED_COINS:
            c_4h = await fetch_crypto_klines(session, item["symbol"], "4h", 30)
            if not c_4h:
                continue
            cur_p = float(c_4h[-1][4])
            c_closes = [float(c[4]) for c in c_4h]
            s_str, r_str = calculate_dynamic_sr(c_4h, cur_p)
            
            tf_results = []
            for lbl, inter in TIMEFRAMES:
                if inter == "4h":
                    sig = evaluate_signal(c_closes)
                else:
                    sub_c = await fetch_crypto_klines(session, item["symbol"], inter, 25)
                    sig = evaluate_signal([float(c[4]) for c in sub_c]) if sub_c else "⚪"
                tf_results.append(f"{lbl}:{'🟢' if 'AL' in sig else ('🔴' if 'SAT' in sig else '⚪')}")

            if item["btc_pair"]:
                b_c = await fetch_crypto_klines(session, item["btc_pair"], "4h", 25)
                b_ratio = float(b_c[-1][4]) if b_c else 0
                b_sig = evaluate_signal([float(c[4]) for c in b_c]) if b_c else "⚪ NÖTR"
                parity_text = f"<code>{b_ratio:.8f} BTC</code>"
            else:
                synth = (cur_p / btc_price) if btc_price > 0 else 0
                parity_text = f"<code>{synth:.8f} BTC</code> (Sentetik)"
                b_sig = "⚡ Sentetik"

            p_note = "🔥 BTC'den Güçlü" if "AL" in b_sig else ("❄️ BTC'den Zayıf" if "SAT" in b_sig else "⚖️ BTC ile Paralel")

            cards.append(
                f"💎 <b>{item['name']}</b>\n"
                f"💰 Fiyat: <code>${cur_p:,.4f}</code> | RSI: <b>{calculate_rsi(c_closes):.1f}</b>\n"
                f"📈 <b>6 Zaman Dilimi:</b> {' | '.join(tf_results)}\n"
                f"🛡️ Destek: <code>{s_str}</code> | 🎯 Direnç: <code>{r_str}</code>\n"
                f"⚡ <b>BTC Oranı:</b> {parity_text} ({b_sig})\n"
                f"💬 {p_note}\n"
            )
        return cards

bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler()

async def send_market_report(chat_id):
    await bot.send_message(chat_id, "⏳ <b>Veriler Taranıyor...</b>")
    cards = await build_full_report()
    for card in cards:
        await bot.send_message(chat_id, card)
        await asyncio.sleep(0.5)

async def scheduled_job():
    global TARGET_CHAT_ID
    if TARGET_CHAT_ID:
        try:
            await send_market_report(TARGET_CHAT_ID)
        except Exception as e:
            logging.error(f"Zamanlanmış bildirim hatası: {e}")

@dp.message(CommandStart())
async def cmd_start(message: Message):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = message.chat.id
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📊 Şimdi Analiz Raporu Al", callback_data="btn_run_analysis")]])
    await message.answer("🚀 <b>Bulut Analiz İstasyonu Aktif!</b>\n\nHer saat :30 geçe otomatik rapor gelecektir.", reply_markup=keyboard)

@dp.message(Command("analiz"))
async def cmd_analiz(message: Message):
    await send_market_report(message.chat.id)

@dp.callback_query(F.data == "btn_run_analysis")
async def callback_analiz(callback: CallbackQuery):
    await callback.answer("Taranıyor...")
    await send_market_report(callback.message.chat.id)

async def web_health_check(request):
    return web.Response(text="Bot 7/24 aktif calisiyor!")

async def main():
    scheduler.add_job(scheduled_job, 'cron', minute=30)
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
