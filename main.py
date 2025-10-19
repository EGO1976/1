import time
import logging
import requests
from flask import Flask, request, jsonify
from binance.client import Client
from binance.error import BinanceAPIException

# === 🔑 ВСТАВЬ СВОИ КЛЮЧИ ===
API_KEY = "ТВОЙ_API_KEY"
API_SECRET = "ТВОЙ_API_SECRET"

# === 🔔 Telegram настройки ===
TELEGRAM_TOKEN = "ТВОЙ_TELEGRAM_BOT_TOKEN"
CHAT_ID = "ТВОЙ_CHAT_ID"  # например, 684398336

def send_telegram(message: str):
    """Отправка сообщений в Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": message})
    except Exception as e:
        logging.error(f"Ошибка Telegram: {e}")

# === Инициализация ===
client = Client(API_KEY, API_SECRET)
app = Flask(__name__)

# === Кэш и защита от дублей ===
positions_cache = {}
active_signals = set()

def get_cached_position(symbol):
    """Получает позицию из кэша или Binance"""
    now = time.time()
    if symbol in positions_cache and now - positions_cache[symbol]["time"] < 5:
        return positions_cache[symbol]["data"]

    try:
        time.sleep(0.3)
        data = client.futures_position_information(symbol=symbol)
        positions_cache[symbol] = {"data": data, "time": now}
        return data
    except BinanceAPIException as e:
        if e.code == -1003:
            logging.warning("🚫 Rate limit Binance! Жду 3 сек...")
            time.sleep(3)
            return None
        else:
            logging.error(f"⚠️ Ошибка Binance при получении позиции: {e}")
            return None


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        logging.info(f"📩 Получен сигнал: {data}")

        symbol = data.get("symbol", "").replace(".P", "")
        side = data.get("side", "").upper()
        notional = float(data.get("amount", 0))
        signal_id = data.get("signalId", "")

        # --- антидубль ---
        if signal_id in active_signals:
            logging.info(f"⚠️ Дубликат сигнала {signal_id}, пропуск")
            return jsonify({"status": "duplicate"}), 200
        active_signals.add(signal_id)

        # --- получить позицию ---
        position_info = get_cached_position(symbol)
        if not position_info:
            return jsonify({"status": "no position info"}), 500

        pos = next((p for p in position_info if p["symbol"] == symbol and p["positionSide"] == "BOTH"), None)
        current_qty = float(pos["positionAmt"]) if pos else 0.0
        mark_price = float(pos["markPrice"]) if pos else 0.0

        # --- если противоположная позиция открыта, закрыть ---
        if (side == "BUY" and current_qty < 0) or (side == "SELL" and current_qty > 0):
            qty_to_close = abs(current_qty)
            close_side = "BUY" if current_qty < 0 else "SELL"
            logging.info(f"🔻 Закрываю {symbol}: {close_side} qty={qty_to_close}")
            try:
                client.futures_create_order(
                    symbol=symbol,
                    side=close_side,
                    type="MARKET",
                    quantity=qty_to_close,
                    reduceOnly=True
                )
                send_telegram(f"🔻 Закрыта позиция {symbol}: {close_side} qty={qty_to_close}")
            except BinanceAPIException as e:
                logging.error(f"Ошибка при закрытии позиции: {e}")

        # --- рассчитать количество ---
        price = mark_price if mark_price > 0 else float(client.futures_mark_price(symbol=symbol)["markPrice"])
        qty = round(notional / price, 3)
        time.sleep(0.2)

        # --- открыть новую позицию ---
        logging.info(f"🚀 Открываю {symbol}: {side} на сумму {notional} USDT (цена={price}, qty={qty})")
        try:
            res = client.futures_create_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=qty,
                reduceOnly=False
            )
            logging.info(f"✅ Позиция открыта: {res}")
            send_telegram(f"✅ {symbol}: {side} на {notional} USDT (qty={qty}) ✅")
        except BinanceAPIException as e:
            logging.error(f"❌ Ошибка открытия позиции: {e}")
            send_telegram(f"❌ Ошибка Binance при открытии {symbol}: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.error(f"⚠️ Ошибка webhook: {e}")
        send_telegram(f"⚠️ Ошибка webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        balance = client.futures_account_balance()
        usdt_balance = next((b for b in balance if b["asset"] == "USDT"), None)
        if usdt_balance:
            logging.info(f"✅ Binance client initialized. USDT balance: {usdt_balance['balance']}")
            send_telegram(f"✅ Сервер запущен. Баланс USDT: {usdt_balance['balance']}")
    except Exception as e:
        logging.warning(f"⚠️ Не удалось получить баланс: {e}")
        send_telegram(f"⚠️ Не удалось получить баланс Binance: {e}")

    logging.info("🚀 Starting server on port 5000")
    app.run(host="0.0.0.0", port=5000)


