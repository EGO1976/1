import os
import json
import math
import logging
import requests
import pytz
from datetime import datetime, timedelta
from flask import Flask, request
from binance.client import Client
from binance.enums import *
from threading import Thread
from time import sleep
import sys

# === НАСТРОЙКИ ===
API_KEY = "***"
API_SECRET = "***"

TELEGRAM_TOKEN = "***"
TELEGRAM_CHAT_ID = "***"

PING_URL = "https://17f0838d-a6eb-4496-885e-a23a4d936f99-00-3neryjhnoljjg.pike.replit.dev"
PING_INTERVAL = 240
PING_TIMEOUT = 15
OPEN_TIMES_FILE = "open_times.json"  # для сохранения времени входа между рестартами

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


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.warning(f"Ошибка отправки в Telegram: {e}")


# === Сохранение/загрузка времени входа ===
def load_open_times():
    if os.path.exists(OPEN_TIMES_FILE):
        try:
            with open(OPEN_TIMES_FILE, "r") as f:
                data = json.load(f)
            return {k: datetime.fromisoformat(v) for k, v in data.items()}
        except Exception:
            return {}
    return {}


def save_open_times():
    try:
        with open(OPEN_TIMES_FILE, "w") as f:
            json.dump({k: v.isoformat() for k, v in open_times.items()}, f)
    except Exception as e:
        logger.warning(f"Ошибка сохранения open_times: {e}")


open_times = load_open_times()

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
        save_open_times()

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
            pnl = (mark_price - entry_price) * qty if pos_amt > 0 else (entry_price - mark_price) * qty

        entry_time = open_times.pop(symbol, datetime.now(tz_kiev))
        save_open_times()
        exit_time = datetime.now(tz_kiev)
        duration = exit_time - entry_time

        # Корректно форматируем время
        days = duration.days
        seconds = duration.seconds
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        time_text = f"{days} дн {hours} ч {minutes} мин {seconds} сек"

        symbol_bold = f"<b>{symbol[:-4]}</b>USDT"
        result_emoji = "🚀" if pnl > 0 else "💔"
        sign = "+" if pnl > 0 else ""

        send_telegram_message(
            f"📉 <b>Позиция закрыта</b>\n"
            f"🔹 {symbol_bold}\n"
            f"{result_emoji} Результат: {sign}{pnl:.2f} USDT\n"
            f"⏱ Время в позиции: {time_text}"
        )

        logger.info(f"Позиция закрыта {symbol}. PnL={pnl:.2f} USDT, время {time_text}")

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


# === Keep-alive + авто-рестарт ===
def keep_alive():
    fails = 0
    while True:
        try:
            r = requests.get(PING_URL, timeout=PING_TIMEOUT)
            if r.status_code == 200:
                logger.info(f"💓 Keep-alive ping OK → {PING_URL}")
                fails = 0
            else:
                raise Exception(f"Status {r.status_code}")
        except Exception as e:
            fails += 1
            logger.warning(f"⚠️ Ошибка пинга ({fails}): {e}")
            if fails >= 3:
                msg = (
                    f"🚨 <b>Replit не отвечает 3 раза подряд!</b>\n"
                    f"⏰ Время: {kiev_time()}\n"
                    f"🔄 Перезапуск через 5 секунд..."
                )
                send_telegram_message(msg)
                logger.error("Replit завис — перезапуск приложения через 5 секунд...")
                sleep(5)
                send_telegram_message(f"✅ <b>Перезапуск выполнен</b>\n⏰ {kiev_time()}")
                sleep(5)
                os.execv(sys.executable, ["python"] + sys.argv)
        sleep(PING_INTERVAL)


# === Запуск ===
if __name__ == "__main__":
    send_telegram_message(f"🚀 <b>Сервер запущен</b>\n⏰ {kiev_time()}")
    Thread(target=keep_alive, daemon=True).start()
    logger.info(f"🚀 Starting server on port 5000 ({kiev_time()})")
    app.run(host="0.0.0.0", port=5000)



















