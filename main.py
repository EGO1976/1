import os
import json
import logging
from flask import Flask, request, jsonify
from binance.client import Client
from binance.exceptions import BinanceAPIException
import requests

# === НАСТРОЙКА ЛОГОВ ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# === ЗАГРУЗКА КЛЮЧЕЙ ===
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TG_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

app = Flask(__name__)

# === TELEGRAM УВЕДОМЛЕНИЯ ===
def send_telegram_message(text):
    """Отправка уведомлений в Telegram"""
    if not TG_TOKEN or not TG_CHAT_ID:
        logging.warning("⚠️ Telegram не настроен (TG_BOT_TOKEN или TG_CHAT_ID отсутствуют)")
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text})
    except Exception as e:
        logging.error(f"Ошибка при отправке в Telegram: {e}")

# === ИНИЦИАЛИЗАЦИЯ BINANCE ===
try:
    client = Client(API_KEY, API_SECRET)
    account = client.futures_account_balance()
    usdt_balance = next((float(x["balance"]) for x in account if x["asset"] == "USDT"), 0.0)
    logging.info(f"✅ Binance client initialized. USDT balance: {usdt_balance}")
except Exception as e:
    logging.error(f"Ошибка инициализации Binance: {e}")

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def get_position_amt(symbol):
    """Получить текущую позицию по символу"""
    try:
        positions = client.futures_position_information(symbol=symbol)
        if positions:
            amt = float(positions[0]["positionAmt"])
            return amt
    except Exception as e:
        logging.error(f"Ошибка получения позиции {symbol}: {e}")
    return 0.0


def close_position(symbol, side, position_amt):
    """Закрыть существующую позицию"""
    try:
        if position_amt == 0:
            return

        close_side = "BUY" if position_amt < 0 else "SELL"
        qty = abs(position_amt)

        res = client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type="MARKET",
            quantity=str(qty),
            reduceOnly=True
        )
        logging.info(f"🔻 Закрываю позицию {symbol}: {close_side} qty={qty}")
        send_telegram_message(f"🔻 Закрыта позиция {symbol}: {close_side} qty={qty}")
        return res
    except BinanceAPIException as e:
        logging.error(f"Binance API ошибка при закрытии позиции: {e}")
    except Exception as e:
        logging.exception(f"Ошибка в close_position: {e}")


def open_position_notional(symbol, side, notional):
    """Открыть позицию по notional (в USDT) с авторасчетом количества"""
    try:
        # Получаем текущую цену
        price_info = client.futures_mark_price(symbol=symbol)
        price = float(price_info["markPrice"])
        qty = round(float(notional) / price, 3)

        logging.info(f"🚀 Открываю позицию {symbol}: {side} на {notional} USDT (цена={price}, qty={qty})")

        # Отправляем ордер с qty
        res = client.futures_create_order(
            symbol=symbol,
            side=side.upper(),
            type="MARKET",
            quantity=str(qty)
        )

        logging.info(f"✅ Позиция открыта: {res}")
        send_telegram_message(f"✅ Открыта позиция {symbol} {side.upper()} qty={qty} (~{notional} USDT)")

    except BinanceAPIException as e:
        logging.error(f"Binance API exception while opening position: {e}")
    except Exception as e:
        logging.exception(f"Ошибка в open_position_notional: {e}")


# === ОБРАБОТЧИК ВЕБХУКА ===
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        logging.info(f"📩 Получен сигнал: {data}")

        symbol = data.get("symbol", "").replace(".P", "")
        side = data.get("side", "").lower()
        notional = float(data.get("amount", 0))
        signal_id = data.get("signalId")

        if not symbol or side not in ("buy", "sell") or not notional:
            return jsonify({"error": "Invalid data"}), 400

        # Получаем текущую позицию
        position_amt = get_position_amt(symbol)
        logging.info(f"Текущая позиция {symbol}: {position_amt}")

        # Закрываем, если есть встречная позиция
        if position_amt != 0 and (
            (side == "buy" and position_amt < 0) or
            (side == "sell" and position_amt > 0)
        ):
            close_position(symbol, side, position_amt)

        # Открываем новую позицию
        open_position_notional(symbol, side, notional)
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.exception("Ошибка обработки вебхука")
        return jsonify({"error": str(e)}), 500


@app.route("/", methods=["GET"])
def index():
    return "✅ Binance Futures Webhook сервер активен!"


# === ЗАПУСК СЕРВЕРА ===
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logging.info(f"Starting server on port {port}")
    app.run(host="0.0.0.0", port=port)












