import os
import time
import math
import logging
import requests
from flask import Flask, request, jsonify
from binance.client import Client
from binance.exceptions import BinanceAPIException

# -----------------------
# Logger
# -----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webhook-server")

# -----------------------
# Read & sanitize env vars (try multiple names)
# -----------------------
def read_env(*names):
    for n in names:
        v = os.getenv(n)
        if v:
            # strip whitespace and control chars
            return v.strip()
    return ""

API_KEY = read_env("API_KEY", "BINANCE_API_KEY", "BINANCE_KEY")
API_SECRET = read_env("API_SECRET", "BINANCE_API_SECRET", "BINANCE_SECRET")
TELEGRAM_TOKEN = read_env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = read_env("TELEGRAM_CHAT_ID")

# -----------------------
# Globals
# -----------------------
app = Flask(__name__)
_client = None
_positions_cache = {}       # symbol -> {data, time}
_processed_signals = {}     # signalId -> timestamp
SIGNAL_KEEP_SECONDS = 3600
POLL_INTERVAL = 1.0
WAIT_CLOSE_TIMEOUT = 30

# -----------------------
# Helper: Telegram
# -----------------------
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.debug("Telegram not configured, skip message.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=6)
    except Exception as e:
        log.error("Telegram send error: %s", e)

# -----------------------
# Lazy Binance client init
# -----------------------
def get_client():
    global _client
    if _client:
        return _client
    if not API_KEY or not API_SECRET:
        log.warning("Binance API keys missing. Set API_KEY and API_SECRET in env.")
        return None
    try:
        # strip again just in case
        _client = Client(API_KEY.strip(), API_SECRET.strip())
        log.info("✅ Binance client initialized")
        return _client
    except Exception as e:
        log.exception("Failed to init Binance client: %s", e)
        _client = None
        return None

# -----------------------
# Utils: caching, rounding
# -----------------------
def cleanup_old_signals():
    now = time.time()
    for k, t in list(_processed_signals.items()):
        if now - t > SIGNAL_KEEP_SECONDS:
            _processed_signals.pop(k, None)

def get_position_amount(symbol: str):
    """Return positionAmt (float) for symbol. Caches for 4-5s."""
    client = get_client()
    if not client:
        raise RuntimeError("binance_client_not_initialized")
    now = time.time()
    cache = _positions_cache.get(symbol)
    if cache and now - cache["time"] < 5:
        return cache["amt"], cache.get("entry", 0.0)
    try:
        time.sleep(0.20)   # small delay to reduce burst rate
        info = client.futures_position_information(symbol=symbol)
        if not info:
            return 0.0, 0.0
        # usually first item has our symbol
        item = info[0]
        amt = float(item.get("positionAmt", 0.0))
        entry = float(item.get("entryPrice", 0.0))
        _positions_cache[symbol] = {"amt": amt, "entry": entry, "time": now}
        return amt, entry
    except BinanceAPIException as e:
        # rate limit code often -1003
        log.error("Binance API error getting position for %s: %s", symbol, e)
        raise
    except Exception as e:
        log.exception("Error getting position: %s", e)
        raise

def wait_for_close(symbol: str, timeout=WAIT_CLOSE_TIMEOUT):
    start = time.time()
    while time.time() - start < timeout:
        try:
            amt, _ = get_position_amount(symbol)
            log.info("Polling positionAmt for %s = %s", symbol, amt)
            if abs(amt) < 1e-8:
                return True
        except Exception as e:
            log.error("Error while polling position: %s", e)
        time.sleep(POLL_INTERVAL)
    return False

def round_qty_to_step(qty: float, step_size: str):
    try:
        step = float(step_size)
        if step == 0:
            return qty
        rounded = math.floor(qty / step) * step
        return float(f"{rounded:.8f}")
    except Exception:
        return qty

def get_symbol_exchange_info(symbol: str):
    client = get_client()
    if not client:
        return None
    try:
        ex = client.futures_exchange_info()
        for s in ex.get("symbols", []):
            if s.get("symbol") == symbol:
                return s
    except Exception as e:
        log.exception("Error fetching exchange info: %s", e)
    return None

# -----------------------
# Core actions: close full, open by notional
# -----------------------
def close_position_full(symbol: str, position_amt: float):
    client = get_client()
    if not client:
        return {"status": "no_client"}
    if position_amt == 0:
        return {"status": "no_position"}
    close_side = "SELL" if position_amt > 0 else "BUY"
    qty = abs(position_amt)
    s_info = get_symbol_exchange_info(symbol)
    if s_info:
        for f in s_info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                qty = round_qty_to_step(qty, f.get("stepSize"))
                break
    try:
        log.info("Placing reduceOnly close order: %s %s qty=%s", symbol, close_side, qty)
        res = client.futures_create_order(symbol=symbol, side=close_side, type="MARKET", quantity=str(qty), reduceOnly=True)
        log.info("Close order response: %s", res)
        return {"status": "placed", "order": res}
    except BinanceAPIException as e:
        log.exception("Binance API exception while placing close order: %s", e)
        return {"status": "error", "error": str(e)}
    except Exception as e:
        log.exception("Exception while placing close order: %s", e)
        return {"status": "error", "error": str(e)}

def open_position_notional(symbol: str, side: str, notional: float):
    client = get_client()
    if not client:
        return {"status": "no_client"}
    try:
        log.info("Opening new position: %s %s notional=%s", symbol, side, notional)
        # Use quoteOrderQty if supported (preferred)
        res = client.futures_create_order(symbol=symbol, side=side, type="MARKET", quoteOrderQty=str(notional))
        log.info("Open order response: %s", res)
        return {"status": "placed", "order": res}
    except BinanceAPIException as e:
        log.exception("Binance API exception while opening position: %s", e)
        return {"status": "error", "error": str(e)}
    except Exception as e:
        log.exception("Exception while opening position: %s", e)
        return {"status": "error", "error": str(e)}

# -----------------------
# Flask endpoints
# -----------------------
@app.route("/", methods=["GET"])
def home():
    return "<h3>Binance Signal Receiver — Server is running</h3><p>POST JSON to /webhook</p>"

@app.route("/webhook", methods=["POST"])
def webhook():
    if get_client() is None:
        msg = "binance_client_not_initialized: set API_KEY and API_SECRET in env"
        log.error(msg)
        return jsonify({"error": msg}), 500

    try:
        payload = request.get_json(force=True)
        log.info("Received webhook: %s", payload)

        raw_symbol = payload.get("symbol") or payload.get("ticker") or ""
        if not raw_symbol:
            return jsonify({"error": "no symbol"}), 400
        # normalize simple suffixes
        symbol = str(raw_symbol).upper().replace(".P", "").replace(".PERP", "").split(":")[-1]

        side = str(payload.get("side", "")).upper()
        if side not in ("BUY", "SELL"):
            return jsonify({"error": "invalid side"}), 400

        amount_field = payload.get("amount") or payload.get("notional") or payload.get("quote") or "0"
        try:
            notional = float(amount_field)
        except Exception:
            notional = 0.0

        signal_id = str(payload.get("signalId") or "")
        now = time.time()
        cleanup_old_signals()

        # dedupe by signalId
        if signal_id:
            if signal_id in _processed_signals:
                log.warning("SignalId %s already processed -> ignoring", signal_id)
                return jsonify({"status": "ignored_duplicate_signal", "signalId": signal_id}), 200

        # quick position check
        try:
            pos_amt, entry = get_position_amount(symbol)
        except Exception as e:
            log.exception("Failed to query position for %s: %s", symbol, e)
            return jsonify({"error": "failed_get_position", "detail": str(e)}), 500

        log.info("Symbol %s incoming side=%s notional=%s current positionAmt=%s signalId=%s", symbol, side, notional, pos_amt, signal_id)

        # if reduceOnly flag present -> close only
        reduce_only_flag = bool(payload.get("reduceOnly", False))
        if reduce_only_flag:
            need_close = (pos_amt > 0 and side == "SELL") or (pos_amt < 0 and side == "BUY")
            if not need_close:
                if signal_id:
                    _processed_signals[signal_id] = now
                return jsonify({"status": "no_op", "reason": "no_opposite_position", "symbol": symbol}), 200
            close_res = close_position_full(symbol, pos_amt)
            if close_res.get("status") == "error":
                return jsonify({"error": "close_failed", "detail": close_res}), 500
            closed = wait_for_close(symbol)
            if not closed:
                return jsonify({"error": "close_timeout"}), 500
            if signal_id:
                _processed_signals[signal_id] = now
            send_telegram(f"Closed {symbol} via reduceOnly (signalId={signal_id})")
            return jsonify({"status": "closed", "symbol": symbol}), 200

        # not reduceOnly: normal flow
        need_close = (pos_amt > 0 and side == "SELL") or (pos_amt < 0 and side == "BUY")
        if need_close:
            close_res = close_position_full(symbol, pos_amt)
            if close_res.get("status") == "error":
                return jsonify({"error": "close_failed", "detail": close_res}), 500
            closed = wait_for_close(symbol)
            if not closed:
                return jsonify({"error": "close_timeout"}), 500

        if notional <= 0:
            if signal_id:
                _processed_signals[signal_id] = now
            return jsonify({"error": "invalid_notional", "detail": f"notional must be >0 (received: {amount_field})"}), 400

        open_res = open_position_notional(symbol, side, notional)
        if open_res.get("status") == "error":
            return jsonify({"error": "open_failed", "detail": open_res}), 500

        # mark processed
        if signal_id:
            _processed_signals[signal_id] = now

        send_telegram(f"Opened {symbol} {side} notional={notional} (signalId={signal_id})")
        return jsonify({"status": "ok", "symbol": symbol, "side": side, "notional": notional}), 200

    except Exception as e:
        log.exception("Unhandled exception in webhook: %s", e)
        return jsonify({"error": "internal_error", "detail": str(e)}), 500

# -----------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    log.info("Starting server on port %s", port)
    app.run(host="0.0.0.0", port=port)










