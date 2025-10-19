import os
import json
import logging
import requests
from flask import Flask, request
from binance.client import Client
from binance.exceptions import BinanceAPIException

# === НАСТРОЙКИ ===
API_KEY = "ВСТАВЬ_СВОЙ_API_KEY"
API_SECRET = "ВСТАВЬ_СВОЙ_API_SECRET"
TELEGRAM_TOKEN = "ВСТАВЬ_СВОЙ_ТЕЛЕГРАМ_ТОКЕН"
TELEGRAM_CHAT_ID = "684398336"

# === ЛОГИ ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)

# === ИНИЦИАЛИЗАЦИЯ BINANCE ===
try:
    client = Client(API_KEY.strip(), API_SECRET.strip())
    balance = client.futures_account_balance()
    usdt_balance = next((b["balance"] for b in balance if b["asset"] == "USDT"), "0")
    logging.info(f"✅ Binance client initialized. USDT balance: {usdt_balance}")
except Exception as e:
    logging.error(f"Ошибка инициализации Binance: {e}")
    client = None


# === ТЕЛЕГРАМ ===
def send_telegram(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        logging.error(f"Ошибка отправки в Telegram: {e}")


# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def get_position(symbol):
    """Получает размер текущей позиции по символу."""
    try:
        pos_info = client.futures_position_information(symbol=symbol)
        position_amt = float(pos_info[0]["positionAmt"])
        return position_amt
    except Exception as e:
        logging.error(f"Ошибка получения позиции {symbol}: {e}")
        return 0.0


def get_price(symbol):
    """Получает текущую рыночную цену символа."""
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        return float(ticker["price"])
    except Exception as e:
        logging.error(f"Ошибка получения цены {symbol}: {e}")
        return 0.0


def open_position(symbol, side, amount_usdt):
    """Открывает позицию на указанную сумму в USDT."""
    price = get_price(symbol)
    if price == 0:
        logging.error("Цена = 0, невозможно рассчитать количество.")
        return
    qty = round(amount_usdt / price, 3)
    try:
        res = client.futures_create_order(
            symbol=symbol,
            side=side.upper(),
            type="MARKET",
            quantity=qty,
            positionSide="BOTH",
        )
        logging.info(f"✅ Открыта позиция {side} {symbol} qty={qty}")
        send_telegram(f"✅ Открыта позиция {side} {symbol} на сумму ≈ {amount_usdt} USDT ({qty} шт)")
    except BinanceAPIException as e:
        logging.error(f"Ошибка открытия позиции: {e}")


def close_position(symbol, qty, side):
    """Закрывает позицию обратным ордером."""
    try:
        res = client.futures_create_order(
            symbol=symbol,
            side="BUY" if side.upper() == "SELL" else "SELL",
            type="MARKET",
            quantity=abs(qty),
            positionSide="BOTH",
            reduceOnly=True,
        )
        logging.info(f"✅ Закрыта позиция {symbol} qty={qty}")
        send_telegram(f"✅ Закрыта позиция {symbol} qty={qty}")
    except BinanceAPIException as e:
        logging.error(f"Ошибка закрытия позиции: {e}")


# === ВЕБХУК ===
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    logging.info(f"📩 Получен сигнал: {data}")

    try:
        symbol = data.get("symbol", "").replace(".P", "")
        side = data.get("side", "").lower()
        amount = float(data.get("amount", 0))
        if not symbol or amount <= 0:
            return "Некорректные данные", 400

        current_pos = get_position(symbol)
        if current_pos != 0:
            logging.info(f"🔄 Закрываю старую позицию {symbol}: {current_pos}")
            close_position(symbol, current_pos, side)

        open_position(symbol, side, amount)
        return "ok", 200

    except Exception as e:
        logging.error(f"Ошибка обработки вебхука: {e}")
        return "error", 500


@app.route("/")
def index():
    return "🚀 Binance Webhook Server работает!"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)






