import os
import json
import math
import logging
from flask import Flask, request
from binance.client import Client
from binance.enums import *
import requests
from time import sleep

# ============ НАСТРОЙКИ ============

API_KEY = "irGJESZl9zdYozc91CtkS2Se703fwZYx0akYCZ6p2f16XLK1AkwGUXLgocO2RXnd"
API_SECRET = "Vk992KplAFUTebPSyGGgwVtODDH9AeTocjtWlpWhaC3zwA3lmmLkoL5mViVmmarF"

# --- ТЕЛЕГРАМ ---
TELEGRAM_TOKEN = "8247871661:AAHWuhS6jkVv-DkYVZDiMLxq5JLwHwLTpBM"
TELEGRAM_CHAT_ID = "684398336"

# ===================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger()

# Инициализация Binance
client = Client(API_KEY, API_SECRET)
try:
    balance = client.futures_account_balance()
    usdt_balance = next(b["balance"] for b in balance if b["asset"] == "USDT")
    logger.info(f"✅ Binance client initialized. USDT balance: {usdt_balance}")
except Exception as e:
    logger.error(f"Ошибка инициализации Binance: {e}")
    usdt_balance = 0


# === Телеграм функция ===
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


# === Вспомогательные функции ===
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
        logger.warning(f"Не удалось получить позицию: {e}")
        return 0.0, 0.0


def open_position(symbol, side, notional_amount):
    price = get_symbol_price(symbol)
    if not price:
        logger.error(f"❌ Не удалось получить цену {symbol}")
        return

    qty = round(float(notional_amount) / price, 0)
    logger.info(
        f"🚀 Открываю позицию {symbol}: {side} на сумму {notional_amount} USDT (цена={price}, qty={qty})"
    )

    order_side = SIDE_BUY if side.lower() == "buy" else SIDE_SELL
    try:
        res = client.futures_create_order(
            symbol=symbol.replace(".P",
                                  ""),  # Binance Futures не принимает ".P"
            type=ORDER_TYPE_MARKET,
            side=order_side,
            quantity=qty,
            reduceOnly=False,
            positionSide="BOTH",
        )
        logger.info(f"✅ Позиция открыта: {res}")

        send_telegram_message(f"📈 <b>Открыта позиция</b>\n"
                              f"🔹 {symbol}\n"
                              f"➡️ {side.upper()}\n"
                              f"💰 Сумма: {notional_amount} USDT")

    except Exception as e:
        logger.error(f"Ошибка открытия позиции: {e}")


def close_position(symbol, side):
    pos_amt, entry_price = get_position(symbol)
    if pos_amt == 0:
        logger.info(f"Нет открытой позиции по {symbol}")
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
        logger.info(f"✅ Позиция закрыта: {res}")

        # PnL расчёт
        mark_price = get_symbol_price(symbol)
        pnl = 0.0
        try:
            if entry_price > 0 and mark_price:
                if pos_amt > 0:  # была LONG
                    pnl = (mark_price - entry_price) * qty
                else:  # была SHORT
                    pnl = (entry_price - mark_price) * qty
        except:
            pass

        send_telegram_message(f"📉 <b>Позиция закрыта</b>\n"
                              f"🔹 {symbol}\n"
                              f"💰 Результат: {pnl:.2f} USDT")

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
    return "Binance Futures Webhook Server работает!"


if __name__ == "__main__":
    logger.info("🚀 Starting server on port 5000")
    app.run(host="0.0.0.0", port=5000)
