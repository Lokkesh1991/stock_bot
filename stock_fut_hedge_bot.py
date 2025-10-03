print("🚀 Starting tradingview_zerodha_ver5_without_hedge...")

from flask import Flask, request, jsonify
from kiteconnect import KiteConnect
import logging
import os
import json
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv
import re
import time

# === Load .env ===
load_dotenv()
API_KEY = os.getenv("KITE_API_KEY")

app = Flask(__name__)

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/tradingview_zerodha.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

# ---------------- Config ----------------
ALLOWED_TF = {"5m", "10m", "15m"}  # accept 5m/10m/15m alerts
COMMODITY_ROOTS = {"NATGASMINI", "NATURALGAS", "COPPER", "GOLDM"}  # MCX

signals = {}
lot_size_cache = {}

@app.route("/")
def home():
    return "✅ Botelyes Trading Webhook (Unified: Stocks + MCX) is Running!"

def get_kite_client():
    try:
        with open("token.json") as f:
            token_data = json.load(f)
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(token_data["access_token"])
        return kite
    except Exception as e:
        logging.error(f"❌ Failed to initialize Kite client: {str(e)}")
        return None

# ---------- Helpers: symbol parsing & instrument lookup ----------
def parse_tv_symbol(raw_symbol: str) -> str:
    """
    Turn TradingView symbol like 'MCX:NATGASMINI1!' or 'NSE:BHEL' into a clean root: 'NATGASMINI' / 'BHEL'
    """
    s = (raw_symbol or "").strip().upper()
    if ":" in s:
        _, s = s.split(":", 1)  # drop exchange prefix
    # remove timeframe/suffixes like '1!' and any non-letters
    s = re.sub(r'[^A-Z]', '', s)
    return s

def resolve_exchange(root: str) -> str:
    """
    If it's one of your commodities → MCX, else assume equity futures → NFO
    """
    return "MCX" if root in COMMODITY_ROOTS else "NFO"

def load_instruments(kite, exchange: str):
    # cache key per exchange would be fine; simple direct call is okay too
    return kite.instruments(exchange)

def expiry_date(i):
    try:
        return datetime.strptime(i["expiry"], "%Y-%m-%d").date()
    except Exception:
        return None

def find_nearest_future(kite, exchange: str, root: str):
    """
    Find the nearest-not-yet-expired FUT instrument for a given root on MCX or NFO.
    Works for MCX commodities and equity futures like BHEL.
    """
    try:
        instr = load_instruments(kite, exchange)
        today = datetime.now().date()

        # Filter futures for this root. Zerodha uses 'tradingsymbol' pattern like ROOTYYMONFUT.
        # We'll match startswith(root) and endswith('FUT')
        cands = [x for x in instr
                 if x.get("instrument_type") == "FUT"
                 and x.get("tradingsymbol","").startswith(root)
                 and x.get("tradingsymbol","").endswith("FUT")]

        # Prefer not-yet-expired, then nearest expiry
        future = [x for x in cands if expiry_date(x) and expiry_date(x) >= today]
        if not future:
            future = cands  # fallback

        future.sort(key=lambda x: expiry_date(x) or today)
        return future[0] if future else None
    except Exception as e:
        logging.error(f"❌ find_nearest_future error ({exchange}/{root}): {e}")
        return None

def get_active_contract(kite, tv_symbol_raw: str):
    """
    Returns (exchange, tradingsymbol) tuple or (None, None)
    """
    root = parse_tv_symbol(tv_symbol_raw)
    exchange = resolve_exchange(root)
    inst = find_nearest_future(kite, exchange, root)
    if not inst:
        logging.error(f"❌ Could not resolve active FUT for {root} on {exchange}")
        return None, None
    return exchange, inst["tradingsymbol"]

def get_lot_size(kite, exchange: str, tradingsymbol: str) -> int:
    key = f"{exchange}:{tradingsymbol}"
    if key in lot_size_cache:
        return lot_size_cache[key]
    try:
        instr = load_instruments(kite, exchange)
        for item in instr:
            if item["tradingsymbol"] == tradingsymbol:
                lot = int(item.get("lot_size", 1))
                lot_size_cache[key] = lot
                return lot
        logging.warning(f"⚠️ Lot size not found for {exchange}:{tradingsymbol} → default 1")
        return 1
    except Exception as e:
        logging.error(f"❌ Error fetching lot size: {e}")
        return 1

# ---------- Order helpers ----------
def enter_position(kite, exchange: str, fut_symbol: str, side: str):
    lot_qty = get_lot_size(kite, exchange, fut_symbol)
    txn = kite.TRANSACTION_TYPE_BUY if side == "LONG" else kite.TRANSACTION_TYPE_SELL
    kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=exchange,
        tradingsymbol=fut_symbol,
        transaction_type=txn,
        quantity=lot_qty,
        product=kite.PRODUCT_NRML,
        order_type=kite.ORDER_TYPE_MARKET
    )
    logging.info(f"✅ Entered {side} {exchange}:{fut_symbol} qty={lot_qty}")
    log_data = {
        "symbol": fut_symbol,
        "exchange": exchange,
        "direction": side,
        "entry_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "qty": lot_qty
    }
    with open(f"logs/{fut_symbol}_trades.json", "a") as f:
        f.write(json.dumps(log_data) + "\n")

def exit_position(kite, exchange: str, fut_symbol: str, qty: int):
    txn = kite.TRANSACTION_TYPE_SELL if qty > 0 else kite.TRANSACTION_TYPE_BUY
    kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=exchange,
        tradingsymbol=fut_symbol,
        transaction_type=txn,
        quantity=abs(qty),
        product=kite.PRODUCT_NRML,
        order_type=kite.ORDER_TYPE_MARKET
    )
    logging.info(f"✅ Exited {exchange}:{fut_symbol} qty={abs(qty)}")

def get_position_quantity(kite, exchange: str, symbol: str) -> int:
    try:
        positions = kite.positions()["net"]
        for pos in positions:
            if pos.get("exchange") == exchange and pos.get("tradingsymbol") == symbol:
                return int(pos.get("quantity", 0))
        return 0
    except Exception:
        return 0

# ---------- Decision engine ----------
def handle_trade_decision(kite, base_key: str, exchange: str, fut_symbol: str, new_signal: str):
    last_action = signals[base_key].get("last_action", "NONE")
    qty = get_position_quantity(kite, exchange, fut_symbol)

    if new_signal != last_action:
        if qty != 0:
            exit_position(kite, exchange, fut_symbol, qty)
        enter_position(kite, exchange, fut_symbol, new_signal)
        signals[base_key]["last_action"] = new_signal

# ---------- Webhook ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json or {}
        raw_symbol   = data.get("symbol", "")
        signal_in    = (data.get("signal", "") or "").upper()
        timeframe_in = (data.get("timeframe", "") or "").lower()

        # Normalize signal
        if signal_in == "BUY":  signal = "LONG"
        elif signal_in == "SELL": signal = "SHORT"
        else: signal = signal_in

        if signal not in {"LONG","SHORT"}:
            return jsonify({"status":"⚠️ ignored - bad signal", "got": signal_in})

        # Normalize timeframe to Xm
        tf = timeframe_in.replace("minutes","m").replace("min","m")
        if not tf.endswith("m"):
            tf += "m"

        if tf not in ALLOWED_TF:
            return jsonify({"status": f"⚠️ Ignored timeframe {tf}. Allowed: {sorted(ALLOWED_TF)}"})

        base_key = parse_tv_symbol(raw_symbol)  # used as key for state
        kite = get_kite_client()
        if not kite:
            return jsonify({"status":"❌ kite failed"})

        exchange, fut_symbol = get_active_contract(kite, raw_symbol)
        if not exchange or not fut_symbol:
            return jsonify({"status":"❌ could not resolve active future", "raw_symbol": raw_symbol})

        # init state
        if base_key not in signals:
            signals[base_key] = {"tf": {}, "last_action": "NONE"}

        # store last signal per timeframe (if you later want MTF logic)
        signals[base_key]["tf"][tf] = signal

        # Act directly on this timeframe's signal
        handle_trade_decision(kite, base_key, exchange, fut_symbol, signal)

        return jsonify({"status":"✅ processed", "exchange": exchange, "fut": fut_symbol, "signal": signal, "tf": tf})

    except Exception as e:
        logging.exception("webhook error")
        return jsonify({"status":"❌ error", "error": str(e)})

# ---------- Main ----------
if __name__ == "__main__":
    print("📅 Start Time:", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print("📂 Current Directory:", os.getcwd())
    print("📁 Logs Path:", os.path.abspath("logs"))
    print("✅ Flask app running at http://0.0.0.0:5000/webhook (or Railway endpoint)")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
