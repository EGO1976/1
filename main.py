import os
import json
import logging
import requests
import pytz
from datetime import datetime, timedelta
from flask import Flask, request
from binance.client import Client
from binance.enums import *
from threading import Thread
from time import sleep

# === НАСТРОЙКИ ===
API_KEY = os.getenv("BINANCE_API_KEY", "***")
API_SECRET = os.getenv("BINANCE_API_SECRET", "***")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "***")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "***")

# Укажи сюда твой Render-домен
PING_URL = os.getenv("PING_URL", "https://one-uutn.onrender.com")
PING_INTERVAL = 240  # каждые 4 минуты
PING_TIMEOUT = 15

# === Flask ===
app = Flask(__name__)

# === Киевское время ===
tz_kiev = pytz.timezone("Europe/Kiev")


def kiev_time():
    return datetime.now(tz_kiev).strftime("%Y-%m-%d %H:%M:%S")


# === Логи с киевским временем ===
class KievFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.now(tz_kiev).strftime("%Y-%m-%d %H:%M:%S")


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
for handler in logging.getLogger().handlers:
    handler.setFormatter(KievFormatter("%(asctime)s %(levelname)s %(message)s"))

logger = logging.getLogger()

# === Binance ===
client = Client(API_KEY, API_SECRET)
try:
    balance = client.futures_account_balance()
    usdt_balance = next(b["balance"] for b in balance if b["asset"] == "USDT")
    logger.info(f"✅ Binance клиент инициализирован. Баланс USDT: {usdt_balance}")
except Exception as e:
    logger.error(f"Ошибка инициализации Binance: {e}")
    usdt_balance = 0


# === Telegram ===
def send_telegram_message(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.warning(f"Ошибка отправки в Telegram: {e}")


# === Вспомогательные ===
def get_symbol_price(symbol):
    try:
        price = float(client.futures_mark_price(symbol=symbol)["markPrice"])
        return price
    except Exception as e:
        logger.error(f"Ошибка получения цены {symbol}: {e}")
        return None


def get_position(symbol):
    try:
        positions = client.futures_position_information(symbol=symbol)
        pos_amt = float(positions[0]["positionAmt"])
        entry_price = float(positions[0]["entryPrice"])
        return pos_amt, entry_price
    except Exception as e:
        logger.warning(f"Не удалось получить позицию {symbol}: {e}")
        return 0.0, 0.0


open_times = {}


def open_position(symbol, side, notional_amount):
    price = get_symbol_price(symbol)
    if not price:
        logger.error(f"❌ Не удалось получить цену {symbol}")
        return

    qty = round(float(notional_amount) / price, 0)
    order_side = SIDE_BUY if side.lower() == "buy" else SIDE_SELL

    try:
        client.futures_create_order(
            symbol=symbol,
            type=ORDER_TYPE_MARKET,
            side=order_side,
            quantity=qty,
            reduceOnly=False,
            positionSide="BOTH",
        )
        open_times[symbol] = datetime.now(tz_kiev)
        logger.info(f"✅ Позиция открыта: {symbol} {side.upper()} {notional_amount} USDT")

        arrow = "🟢⬆️" if side.lower() == "buy" else "🔴⬇️"
        send_telegram_message(
            f"📈 <b>Открыта позиция</b>\n"
            f"🔹 <b>{symbol[:-4]}</b>USDT\n"
            f"{arrow} {side.upper()}\n"
            f"💰 Сумма: {notional_amount} USDT"
        )

    except Exception as e:
        logger.error(f"Ошибка открытия позиции: {e}")


def close_position(symbol, side):
    pos_amt, entry_price = get_position(symbol)
    if pos_amt == 0:
        logger.info(f"Нет открытой позиции по {symbol}")
        return

    close_side = SIDE_SELL if pos_amt > 0 else SIDE_BUY
    qty = abs(pos_amt)
    mark_price = get_symbol_price(symbol)
    pnl = 0.0

    try:
        client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type=ORDER_TYPE_MARKET,
            quantity=qty,
            reduceOnly=True,
            positionSide="BOTH",
        )

        if entry_price > 0 and mark_price:
            pnl = (mark_price - entry_price) * qty if pos_amt > 0 else (
                entry_price - mark_price) * qty

        entry_time = open_times.pop(symbol, datetime.now(tz_kiev))
        duration = datetime.now(tz_kiev) - entry_time
        days = duration.days
        hours, remainder = divmod(duration.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        result_emoji = "🚀" if pnl > 0 else "💔"
        sign = "+" if pnl > 0 else ""

        send_telegram_message(
            f"📉 <b>Позиция закрыта</b>\n"
            f"🔹 <b>{symbol[:-4]}</b>USDT\n"
            f"{result_emoji} Результат: {sign}{pnl:.2f} USDT\n"
            f"⏱ Время в позиции: {days} дн {hours} ч {minutes} мин {seconds} сек"
        )

        logger.info(f"Позиция закрыта {symbol}. PnL={pnl:.2f} USDT")

    except Exception as e:
        logger.error(f"Ошибка закрытия позиции: {e}")


# === Flask Webhook ===
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data:
        return {"code": "error", "message": "No data"}, 400

    logger.info(f"📩 Получен сигнал: {data}")
    try:
        symbol = data["symbol"].replace(".P", "")
        side = data["side"].lower()
        amount = float(data.get("amount", 50))
        pos_amt, _ = get_position(symbol)

        if side == "buy":
            if pos_amt < 0:
                close_position(symbol, side)
                sleep(1)
            open_position(symbol, side, amount)
        elif side == "sell":
            if pos_amt > 0:
                close_position(symbol, side)
                sleep(1)
            open_position(symbol, side, amount)
    except Exception as e:
        logger.error(f"Ошибка обработки сигнала: {e}")

    return {"code": "success"}, 200


@app.route("/")
def home():
    return f"✅ Server running ({kiev_time()})", 200


# === Keep-alive ===
def keep_alive():
    while True:
        try:
            r = requests.get(PING_URL, timeout=PING_TIMEOUT)
            if r.status_code == 200:
                logger.info(f"💓 Keep-alive ping OK → {PING_URL}")
            else:
                logger.warning(f"⚠️ Keep-alive ping error: {r.status_code}")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка пинга: {e}")
        sleep(PING_INTERVAL)


# === Запуск ===
if __name__ == "__main__":
    Thread(target=keep_alive, daemon=True).start()
    send_telegram_message(f"🚀 <b>Сервер запущен на Render</b>\n⏰ {kiev_time()}")
    logger.info(f"🚀 Server started on port 5000 ({kiev_time()})")
    port = int(os.environ.get("PORT", 5000))
app.run(host="0.0.0.0", port=port)
























