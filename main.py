

# main.py
import os
import time
import math
import logging
import requests
from flask import Flask, request, jsonify
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ----------------------------
# Конфигурация через переменные окружения (не хранить ключи в репозитории)
# ----------------------------
API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

USE_TESTNET = os.environ.get("USE_TESTNET", "false").lower() == "true"

# Настройки
WAIT_CLOSE_TIMEOUT = int(os.environ.get("WAIT_CLOSE_TIMEOUT", "40"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
SIGNAL_DEDUPE_KEEP = int(os.environ.get("SIGNAL_DEDUPE_KEEP", "3600"))

# Логирование
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("binance-webhook")

# Инициализация Binance client
client = None
if API_KEY and API_SECRET:
    try:
        client = Client(API_KEY, API_SECRET, testnet=USE_TESTNET)
        try:
            balances = client.futures_account_balance()
            usdt_bal = next((float(x["balance"]) for x in balances if x["asset"] == "USDT"), None)
            log.info("✅ Binance client initialized. USDT balance: %s", usdt_bal)
        except Exception as e:
            log.warning("Binance client initialized but balance fetch failed: %s", e)
    except Exception as e:
        log.exception("Failed to init Binance client: %s", e)
else:
    log.warning("API_KEY/API_SECRET not provided — client not initialized (set env vars).")

# Flask app
app = Flask(__name__)

# Кэши
_processed_signals = {}
_exchange_info_cache = {}


# ========== Telegram helper ==========
def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.debug("Telegram not configured, skipping message.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        resp = requests.post(url, json=payload, timeout=6)
        if resp.status_code != 200:
            log.warning("Telegram send failed: %s %s", resp.status_code, resp.text)
    except Exception as e:
        log.warning("Telegram send exception: %s", e)


# ========== Helpers for Binance ==========
def clean_symbol(sym: str) -> str:
    if not sym:
        return sym
    s = str(sym).upper().strip()
    for suf in [".P", ".PERP", ".FUT", ":BINANCE", ":BINANCEFUTURES"]:
        s = s.replace(suf, "")
    return s.replace("/", "").split()[0]


def get_symbol_info(symbol: str):
    if symbol in _exchange_info_cache:
        return _exchange_info_cache[symbol]
    try:
        info = client.futures_exchange_info()
        for s in info.get("symbols", []):
            if s.get("symbol") == symbol:
                _exchange_info_cache[symbol] = s
                return s
    except Exception as e:
        log.warning("Error fetching exchange info: %s", e)
    return None


def get_position_amount(symbol: str) -> float:
    try:
        info = client.futures_position_information(symbol=symbol)
        for it in info:
            if it.get("symbol", "").upper() == symbol.upper():
                return float(it.get("positionAmt", 0))
        return 0.0
    except BinanceAPIException as e:
        log.error("Binance API error getting position for %s: %s", symbol, e)
        raise
    except Exception as e:
        log.exception("Error get_position_amount: %s", e)
        raise


def round_step(qty: float, step: str) -> float:
    try:
        step_f = float(step)
        if step_f == 0:
            return qty
        rounded = math.floor(qty / step_f) * step_f
        return float("{:.8f}".format(rounded))
    except Exception:
        return qty


def place_reduceonly_close(symbol: str, position_amt: float):
    if position_amt == 0:
        return {"status": "no_position"}
    close_side = "SELL" if position_amt > 0 else "BUY"
    qty = abs(position_amt)
    s_info = get_symbol_info(symbol)
    if s_info:
        for f in s_info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                qty = round_step(qty, f.get("stepSize"))
                break
    try:
        log.info("Placing reduceOnly close order: %s %s qty=%s", symbol, close_side, qty)
        res = client.futures_create_order(symbol=symbol, side=close_side, type="MARKET", quantity=str(qty), reduceOnly=True)
        log.info("Close order response: %s", res)
        return {"status": "placed", "order": res}
    except BinanceAPIException as e:
        log.exception("Binance API exception placing close order: %s", e)
        return {"status": "error", "error": str(e)}
    except Exception as e:
        log.exception("Exception placing close order: %s", e)
        return {"status": "error", "error": str(e)}


def open_position_notional(symbol: str, side: str, notional: float):
    try:
        log.info("Opening new position: %s %s notional=%s", symbol, side, notional)
        res = client.futures_create_order(symbol=symbol, side=side, type="MARKET", quoteOrderQty=str(notional))
        log.info("Open order response: %s", res)
        return {"status": "placed", "order": res}
    except BinanceAPIException as e:
        log.exception("Binance API exception opening position: %s", e)
        return {"status": "error", "error": str(e)}
    except Exception as e:
        log.exception("Exception opening position: %s", e)
        return {"status": "error", "error": str(e)}


def wait_for_position_closed(symbol: str, timeout=WAIT_CLOSE_TIMEOUT, poll_interval=POLL_INTERVAL):
    log.info("Waiting up to %s sec for position to close for %s ...", timeout, symbol)
    start = time.time()
    while time.time() - start < timeout:
        try:
            amt = get_position_amount(symbol)
            log.info("Current positionAmt for %s = %s", symbol, amt)
            if abs(amt) < 1e-8:
                return True
        except Exception as e:
            log.exception("Error checking position amount: %s", e)
        time.sleep(poll_interval)
    return False


def cleanup_old_processed_signalids():
    now = time.time()
    keys = list(_processed_signals.keys())
    for k in keys:
        if now - _processed_signals[k] > SIGNAL_DEDUPE_KEEP:
            _processed_signals.pop(k, None)


# ===========================
# Routes
# ===========================
@app.route("/", methods=["GET"])
def home():
    return "<h3>Binance Signal Receiver — Server is running</h3><p>POST JSON to /webhook</p>"


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        log.info("Received webhook: %s", data)

        raw_symbol = data.get("symbol") or data.get("ticker") or ""
        if not raw_symbol:
            return jsonify({"error": "no symbol"}), 400
        symbol = clean_symbol(raw_symbol)

        side = str(data.get("side", "")).upper()
        if side not in ("BUY", "SELL"):
            return jsonify({"error": "invalid side"}), 400

        amount_field = data.get("amount") or data.get("notional") or data.get("quote") or data.get("quantity") or 0
        try:
            notional = float(amount_field)
        except Exception:
            notional = 0.0

        signal_id = str(data.get("signalId") or "")
        uid = str(data.get("uid") or "")

        # Dedupe
        cleanup_old_processed_signalids()
        now = time.time()
        dedupe_key = f"{symbol}|{side}|{notional}"
        if signal_id and signal_id in _processed_signals:
            log.warning("SignalId %s already processed -> ignoring", signal_id)
            return jsonify({"status": "ignored_duplicate_signal", "signalId": signal_id}), 200
        if dedupe_key in _processed_signals and now - _processed_signals[dedupe_key] < 1.0:
            log.warning("Rapid duplicate %s -> ignoring", dedupe_key)
            return jsonify({"status": "ignored_quick_duplicate", "key": dedupe_key}), 200

        # get current position
        try:
            pos_amt = get_position_amount(symbol)
        except Exception as e:
            return jsonify({"error": "error_getting_position", "detail": str(e)}), 500

        log.info("Symbol: %s, incoming side=%s, notional=%s, current positionAmt=%s", symbol, side, notional, pos_amt)

        # If reduceOnly -> close only
        reduce_only_flag = bool(data.get("reduceOnly", False))
        if reduce_only_flag:
            need_close = (pos_amt > 0 and side == "SELL") or (pos_amt < 0 and side == "BUY")
            if need_close:
                close_res = place_reduceonly_close(symbol, pos_amt)
                if close_res.get("status") == "error":
                    return jsonify({"error": "close_failed", "detail": close_res}), 500
                closed = wait_for_position_closed(symbol)
                if not closed:
                    return jsonify({"error": "close_timeout"}), 500
                send_telegram_message(f"📉 Closed {symbol} due to reduceOnly (signalId={signal_id})")
            _processed_signals[signal_id or dedupe_key] = now
            return jsonify({"status": "closed_only"}), 200

        # close opposite if exists
        need_close = (pos_amt > 0 and side == "SELL") or (pos_amt < 0 and side == "BUY")
        if need_close:
            close_res = place_reduceonly_close(symbol, pos_amt)
            if close_res.get("status") == "error":
                return jsonify({"error": "close_failed", "detail": close_res}), 500
            closed = wait_for_position_closed(symbol)
            if not closed:
                return jsonify({"error": "close_timeout"}), 500
            # send telegram about close
            send_telegram_message(f"📉 Closed opposite position for {symbol} (signalId={signal_id})")

        # open new position
        if notional <= 0:
            _processed_signals[signal_id or dedupe_key] = now
            return jsonify({"error": "invalid_notional", "detail": amount_field}), 400

        open_result = open_position_notional(symbol, side, notional)
        if open_result.get("status") == "error":
            return jsonify({"error": "open_failed", "detail": open_result}), 500

        # telegram about open
        send_telegram_message(f"📈 Opened {side} {symbol} notional={notional} (signalId={signal_id})")

        # mark processed
        _processed_signals[signal_id or dedupe_key] = now

        return jsonify({"status": "ok", "symbol": symbol, "side": side}), 200

    except Exception as e:
        log.exception("Unhandled exception in webhook: %s", e)
        return jsonify({"error": "internal_error", "detail": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info("Starting server on port %s", port)
    app.run(host="0.0.0.0", port=port)
