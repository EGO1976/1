import logging
from flask import Flask, request, jsonify
from binance.client import Client
from binance.exceptions import BinanceAPIException
import requests
import os
import time

# ===========================
# 🔧 НАСТРОЙКИ
# ===========================
API_KEY = "ВАШ_BINANCE_API_KEY"
API_SECRET = "ВАШ_BINANCE_API_SECRET"

# Токен и ID чата Telegram
TELEGRAM_TOKEN = "ВАШ_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "ВАШ_CHAT_ID"

# ===========================
# ⚙️ НАСТРОЙКА ЛОГГЕРА
# ===========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# ===========================
# 🚀 ИНИЦИАЛИЗАЦИЯ
# ===========================
app = Flask(__name__)
client = Client(API_KEY, API_SECRET)

# Проверяем баланс
try:
    balance = client.futures_account_balance()
    usdt_balance = next(b for b in balance if b["asset"] == "USDT")["balance"]
    logging.info(f"✅ Binance client initialized. USDT balance: {usdt_balance}")
except Exception as e:
    logging.error(f"❌ Binance init error: {e}")

# ===========================
# 💬 TELEGRAM
# ===========================
def send_telegram_message(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        requests.post(url, json=payload)
    except Exception as e:
        logging.error(f"Ошибка отправки в Telegram: {e}")

# ===========================
# 📈 ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ===========================
def get_current_price(symbol):
    price = float(client.futures_symbol_ticker(symbol=symbol)["price"])
    return price

def get_position_info(symbol):
    try:
        positions = client.futures_position_information(symbol=symbol)
        pos = positions[0]
        amt = float(pos["positionAmt"])
        entry = float(pos["entryPrice"])
        return amt, entry
    except BinanceAPIException as e:
        logging.error(f"Binance API error getting position for {symbol}: {e}")
        return 0.0, 0.0

def close_position(symbol, side, qty):
    side_action = "BUY" if side == "SELL" else "SELL"
    try:
        logging.info(f"🔻 Закрываю позицию {symbol}: {side_action} qty={qty}")
        client.futures_create_order(
            symbol=symbol,
            side=side_action,
            type="MARKET",
            quantity=abs(qty),
            reduceOnly=True,
        )
        send_telegram_message(f"🔻 Закрыта позиция {symbol} {side_action} qty={qty}")
    except BinanceAPIException as e:
        logging.error(f"Ошибка закрытия позиции: {e}")

def open_position_by_notional(symbol, side, usdt_amount):
    try:
        price = get_current_price(symbol)
        qty = round(usdt_amount / price, 0)
        logging.info(f"🚀 Открываю позицию {symbol}: {side} на сумму {usdt_amount} USDT (цена={price}, qty={qty})")
        res = client.futures_create_order(
            symbol=symbol,
            side=side.upper(),
            type="MARKET",
            quantity=qty,
            reduceOnly=False,
            positionSide="BOTH"
        )
        send_telegram_message(f"🚀 Открыта позиция {symbol} {side} qty={qty} на сумму {usdt_amount} USDT")
        logging.info(f"✅ Позиция открыта: {res}")
    except BinanceAPIException as e:
        logging.error(f"Ошибка открытия позиции: {e}")

# ===========================
# 🌐 WEBHOOK ОТ TRADINGVIEW
# ===========================
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    logging.info(f"📩 Получен сигнал: {data}")

    try:
        symbol = data["symbol"].replace(".P", "")  # если приходит с ".P"
        side = data["side"].lower()
        amount = float(data["amount"])

        position_amt, entry_price = get_position_info(symbol)
        logging.info(f"Текущая позиция {symbol}: {position_amt}")

        # если есть открытая позиция — закрываем
        if position_amt != 0:
            current_side = "BUY" if position_amt > 0 else "SELL"
            close_position(symbol, current_side, abs(position_amt))

            # прибыль/убыток
            current_price = get_current_price(symbol)
            pnl = (current_price - entry_price) * position_amt
            send_telegram_message(f"💰 {symbol}: Закрыта {current_side} | PnL: {pnl:.2f} USDT")

        # открываем новую позицию
        open_position_by_notional(symbol, side, amount)
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.error(f"Ошибка обработки сигнала: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/')
def home():
    return "Binance Webhook Server работает ✅", 200

# ===========================
# 🚀 ЗАПУСК
# ===========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    logging.info(f"🚀 Starting server on port {port}")
    app.run(host="0.0.0.0", port=port)




