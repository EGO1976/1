import os
import json
import math
import logging
from flask import Flask, request
from binance.client import Client
from binance.enums import *
import requests
import threading
from time import sleep

# ================= НАСТРОЙКИ ==================
API_KEY = os.getenv("BINANCE_API_KEY", "***")
API_SECRET = os.getenv("BINANCE_API_SECRET", "***")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "***")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "***")

# Render автоматически передаёт URL в переменной среды RENDER_EXTERNAL_URL
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", None)
# ==============================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger()

# === Binance ===
try:
    client = Client(API_KEY, API_SECRET)
    balance = client.futures_account_balance()
    usdt_balance = next(b["balance"] for b in balance if b["asset"] == "USDT")
    logger.info(f"✅ Binance клиент инициализирован. Баланс USDT: {usdt_balance}")
except Exception as e:
    logger.error(f"❌ Ошибка инициализации Binance: {e}")
    client = None

# === Telegram ===
def send_telegram_message(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
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
        pos = client.futures_position_information(symbol=symbol)
        pos_amt = float(pos[0]["positionAmt"])
        entry_price = float(pos[0]["entryPrice"])
        return pos_amt, entry_price
    except Exception as e:
        logger.warning(f"Не удалось получить позицию: {e}")
        return 0.0, 0.0

def open_position(symbol, side, notional_amount):
    price = get_symbol_price(symbol)
    if not price:
        return
    qty = round(float(notional_amount) / price, 0)
    logger.info(f"🚀 Открываю позицию {symbol}: {side} на {notional_amount} USDT (цена={price}, qty={qty})")

    order_side = SIDE_BUY if side.lower() == "buy" else SIDE_SELL
    try:
        res = client.futures_create_order(
            symbol=symbol.replace(".P", ""),
            type=ORDER_TYPE_MARKET,
            side=order_side,
            quantity=qty,
            reduceOnly=False,
            positionSide="BOTH",
        )
        logger.info(f"✅ Позиция открыта: {res}")
        send_telegram_message(f"📈 <b>Открыта позиция</b>\n🔹 {symbol}\n➡️ {side.upper()}\n💰 {notional_amount} USDT")
    except Exception as e:
        logger.error(f"Ошибка открытия позиции: {e}")

def close_position(symbol, side):
    pos_amt, entry_price = get_position(symbol)
    if pos_amt == 0:
        return
    close_side = SIDE_SELL if pos_amt > 0 else SIDE_BUY
    qty = abs(pos_amt)
    logger.info(f"🔻 Закрываю позицию {symbol}: {close_side} qty={qty}")
    try:
        res = client.futures_create_order(
            symbol=symbol.replace(".P", ""),
            side=close_side,
            type=ORDER_TYPE_MARKET,
            quantity=qty,
            reduceOnly=True,
            positionSide="BOTH",
        )
        mark_price = get_symbol_price(symbol)
        pnl = (mark_price - entry_price) * qty if pos_amt > 0 else (entry_price - mark_price) * qty
        send_telegram_message(f"📉 <b>Позиция закрыта</b>\n🔹 {symbol}\n💰 Результат: {pnl:.2f} USDT")
    except Exception as e:
        logger.error(f"Ошибка закрытия позиции: {e}")

# === Flask Webhook ===
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
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
    return "✅ Binance Futures Webhook Server активен и не засыпает!"

# === Keep-alive thread ===
def keep_alive():
    # Автоматически подставляем Render URL
    if not RENDER_URL:
        logger.warning("⚠️ Не найден RENDER_EXTERNAL_URL. Keep-alive будет выключен.")
        return
    while True:
        try:
            requests.get(RENDER_URL, timeout=10)
            logger.info(f"💓 Keep-alive ping sent to {RENDER_URL}")
        except Exception as e:
            logger.warning(f"Keep-alive error: {e}")
        sleep(60)

if __name__ == "__main__":
    if RENDER_URL:
        threading.Thread(target=keep_alive, daemon=True).start()
    logger.info("🚀 Starting server on port 5000")
    app.run(host="0.0.0.0", port=5000)

















