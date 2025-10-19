import os
import json
import logging
from flask import Flask, request, jsonify
from binance.client import Client
from binance.exceptions import BinanceAPIException
import requests

# === Настройка логов ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# === Flask ===
app = Flask(__name__)

# === Переменные окружения ===
API_KEY = os.getenv("API_KEY", "").encode("utf-8").decode("utf-8", "ignore")
API_SECRET = os.getenv("API_SECRET", "").encode("utf-8").decode("utf-8", "ignore")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# === Telegram уведомления ===
def send_telegram_message(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram config missing — skipping message.")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=5
        )
    except Exception as e:
        logging.error(f"Ошибка отправки Telegram: {e}")

# === Инициализация Binance ===
client = None
try:
    client = Client(API_KEY, API_SECRET)
    balance = client.futures_account_balance()
    usdt_balance = next((float(b['balance']) for b in balance if b['asset'] == 'USDT'), 0)
    logging.info(f"✅ Binance client initialized. USDT balance: {usdt_balance}")
except Exception as e:
    logging.error(f"Ошибка инициализации Binance: {e}")

# === Webhook ===
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    logging.info(f"📩 Получен сигнал: {data}")

    try:
        symbol = data["symbol"].replace(".P", "")
        side = data["side"].upper()
        amount = float(data["amount"])
        price_type = data.get("price", "market")

        # Закрытие открытых позиций
        positions = client.futures_position_information(symbol=symbol)
        current_pos = float(positions[0]["positionAmt"])
        if current_pos != 0:
            close_side = "SELL" if current_pos > 0 else "BUY"
            client.futures_create_order(
                symbol=symbol, side=close_side, type="MARKET", quantity=abs(current_pos)
            )
            send_telegram_message(f"❌ Закрыта позиция {symbol}: {close_side} {abs(current_pos)}")

        # Открытие новой позиции
        price = client.futures_symbol_ticker(symbol=symbol)["price"]
        qty = round(amount / float(price), 3)
        client.futures_create_order(symbol=symbol, side=side, type="MARKET", quantity=qty)
        send_telegram_message(f"✅ Открыта позиция {symbol}: {side} на {amount} USDT")

        return jsonify({"status": "ok"}), 200

    except BinanceAPIException as e:
        logging.error(f"Binance API error: {e}")
        send_telegram_message(f"⚠️ Binance API error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        logging.error(f"Ошибка обработки webhook: {e}")
        send_telegram_message(f"❗ Ошибка webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/")
def home():
    return "🚀 Binance Webhook Server работает!"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logging.info(f"Starting server on port {port}")
    app.run(host="0.0.0.0", port=port)








