print("🚀 Starting tradingview_zerodha_ver5_without_hedge — NATGASMINI-Only Mode with 4-day rollover...")

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

# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------
ALLOWED_TF = {"5m", "10m", "15m", "20m", "30m", "60m"}
MCX_CANONICAL_ROOTS = {"NATGASMINI"}
ALIAS_TO_CANON = {
    "NATGAS": "NATGASMINI",
    "NATGASMINI": "NATGASMINI",
    "NATURALGAS": "NATGASMINI"
}
ROLLOVER_DAYS = 4  # Auto-rollover 4 days before expiry

signals = {}
lot_size_cache = {}

@app.route("/")
def home():
    return "✅ Botelyes Webhook — NATGASMINI-Only Mode with 4-day rollover!"

# --------------------------------------------------------------------
# Kite client
# --------------------------------------------------------------------
def get_kite_client():
    try:
        with open("token.json") as f:
            token_data = json.load(f)
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(token_data["access_token"])
        return kite
    except Exception as e:
        logging.error(f"❌ Failed to initialize Kite client: {e}")
        return None

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def normalize_timeframe(tf_in: str) -> str:
    s = (tf_in or "").strip().lower()
    s = s.replace("minutes", "m").replace("minute", "m").replace("mins", "m").replace("min", "m")
    s = s.replace(" ", "")
    if re.fullmatch(r"\d+", s): s = f"{s}m"
    m = re.fullmatch(r"(\d+)h", s)
    if m: s = f"{int(m.group(1))*60}m"
    if not s.endswith("m"): s += "m"
    if s == "1m0m": s = "10m"
    if s == "60m": return "60m"
    return s

def parse_tv_symbol(raw_symbol: str) -> str:
    s = (raw_symbol or "").upper().strip()
    if ":" in s: _, s = s.split(":", 1)
    s = s.replace("1!", "").replace("2!", "").replace("3!", "").replace("!", "")
    s = re.sub(r"[^A-Z]", "", s)
    return ALIAS_TO_CANON.get(s, s)

def load_instruments(kite, exchange: str):
    return kite.instruments(exchange)

def expiry_date(i):
    try:
        return datetime.strptime(i["expiry"], "%Y-%m-%d").date()
    except Exception:
        return None

def find_all_futures(kite, exchange: str, root: str):
    instr = load_instruments(kite, exchange)
    futs = []
    for x in instr:
        ts = x.get("tradingsymbol", "")
        if x.get("instrument_type") == "FUT" and ts.startswith(root) and ts.endswith("FUT"):
            ed = expiry_date(x)
            if ed:
                futs.append((ed, x))
    futs.sort(key=lambda t: t[0])
    return futs

def days_to_expiry(kite, exchange, symbol):
    instr = load_instruments(kite, exchange)
    for x in instr:
        if x["tradingsymbol"] == symbol:
            ed = expiry_date(x)
            if not ed:
                return None
            return (ed - datetime.now().date()).days
    return None

def maybe_rollover_existing(kite, exchange, root, symbol):
    """If current month has <=4 days to expiry, close and reopen next-month FUT."""
    dte = days_to_expiry(kite, exchange, symbol)
    if dte is None or dte > ROLLOVER_DAYS:
        return symbol

    futs = find_all_futures(kite, exchange, root)
    if len(futs) < 2:
        logging.warning(f"⚠️ No next-month FUT found for {root}")
        return symbol

    next_symbol = futs[1][1]["tradingsymbol"]
    qty = get_position_quantity(kite, exchange, symbol)
    if qty == 0:
        return next_symbol

    side = "LONG" if qty > 0 else "SHORT"
    logging.info(f"🔄 Rollover: {symbol} (DTE={dte}) → {next_symbol} ({side})")
    exit_position(kite, exchange, symbol, qty)
    enter_position(kite, exchange, next_symbol, side)
    return next_symbol

# --- NATGAS detect ---
_LETTER_MONTH = set("FGHJKMNQUVXZ")
def looks_like_natgas_letter_year(s: str) -> bool:
    s = s.split(":", 1)[-1]
    m = re.search(r"NATGAS([A-Z])20\d{2}", s)
    return bool(m and m.group(1) in _LETTER_MONTH)

def is_natgas_continuous(s: str) -> bool:
    s = s.split(":", 1)[-1]
    return s.startswith("NATGAS1") or s.startswith("NATGASMINI1")

def get_active_contract(kite, tv_symbol_raw: str):
    raw_upper = (tv_symbol_raw or "").upper().strip()
    if is_natgas_continuous(raw_upper) or looks_like_natgas_letter_year(raw_upper) or "NATGAS" in raw_upper:
        exchange = "MCX"
        root = "NATGASMINI"
        futs = find_all_futures(kite, exchange, root)
        if not futs:
            logging.error("❌ No active MCX FUT found for NATGASMINI")
            return None, None
        cur_symbol = futs[0][1]["tradingsymbol"]
        dte = (futs[0][0] - datetime.now().date()).days
        if dte <= ROLLOVER_DAYS and len(futs) > 1:
            logging.info(f"📦 New entries → next FUT since DTE={dte}≤{ROLLOVER_DAYS}")
            cur_symbol = futs[1][1]["tradingsymbol"]
        logging.info(f"🟢 NATGAS alert mapped → {cur_symbol}")
        return exchange, cur_symbol
    return None, None

# --------------------------------------------------------------------
# Order helpers
# --------------------------------------------------------------------
def get_lot_size(kite, exchange: str, tradingsymbol: str) -> int:
    key = f"{exchange}:{tradingsymbol}"
    if key in lot_size_cache: return lot_size_cache[key]
    try:
        instr = load_instruments(kite, exchange)
        for x in instr:
            if x["tradingsymbol"] == tradingsymbol:
                lot = int(x.get("lot_size", 1))
                lot_size_cache[key] = lot
                return lot
        return 1
    except Exception:
        return 1

def enter_position(kite, exchange, fut_symbol, side):
    qty = get_lot_size(kite, exchange, fut_symbol)
    txn = kite.TRANSACTION_TYPE_BUY if side == "LONG" else kite.TRANSACTION_TYPE_SELL
    kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=exchange,
        tradingsymbol=fut_symbol,
        transaction_type=txn,
        quantity=qty,
        product=kite.PRODUCT_NRML,
        order_type=kite.ORDER_TYPE_MARKET
    )
    logging.info(f"✅ Entered {side} {exchange}:{fut_symbol} qty={qty}")

def exit_position(kite, exchange, fut_symbol, qty):
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

def get_position_quantity(kite, exchange, symbol):
    try:
        positions = kite.positions()["net"]
        for p in positions:
            if p.get("exchange") == exchange and p.get("tradingsymbol") == symbol:
                return int(p.get("quantity", 0))
        return 0
    except Exception:
        return 0

# --------------------------------------------------------------------
# Decision engine
# --------------------------------------------------------------------
def handle_trade_decision(kite, base_key, exchange, fut_symbol, new_signal):
    qty = get_position_quantity(kite, exchange, fut_symbol)
    side = "LONG" if qty > 0 else ("SHORT" if qty < 0 else "FLAT")

    # Auto-rollover if needed
    fut_symbol = maybe_rollover_existing(kite, exchange, "NATGASMINI", fut_symbol)

    if (side == "LONG" and new_signal == "LONG") or (side == "SHORT" and new_signal == "SHORT"):
        logging.info(f"🟡 No-op: already {side} on {fut_symbol}")
        signals[base_key]["last_action"] = new_signal
        return

    if side != "FLAT" and side != new_signal:
        exit_position(kite, exchange, fut_symbol, qty)
    if side == "FLAT" or side != new_signal:
        enter_position(kite, exchange, fut_symbol, new_signal)

    signals[base_key]["last_action"] = new_signal

# --------------------------------------------------------------------
# Webhook
# --------------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json or {}
        raw_symbol = data.get("symbol", "")
        signal_in = (data.get("signal", "") or "").upper()
        timeframe_in = (data.get("timeframe", "") or "")

        if "NATGAS" not in raw_symbol.upper():
            logging.info(f"🚫 Ignored non-NATGAS symbol: {raw_symbol}")
            return jsonify({"status": "🚫 Ignored — not NATGASMINI", "symbol": raw_symbol})

        if signal_in == "BUY": signal = "LONG"
        elif signal_in == "SELL": signal = "SHORT"
        else: signal = signal_in
        if signal not in {"LONG", "SHORT"}:
            return jsonify({"status": "⚠️ Ignored bad signal", "got": signal_in})

        tf = normalize_timeframe(timeframe_in)
        if tf not in ALLOWED_TF:
            return jsonify({"status": f"⚠️ Ignored timeframe {tf}", "allowed": list(ALLOWED_TF)})

        base_key = "NATGASMINI"
        kite = get_kite_client()
        if not kite:
            return jsonify({"status": "❌ Kite init failed"})

        exchange, fut_symbol = get_active_contract(kite, raw_symbol)
        if not exchange or not fut_symbol:
            return jsonify({"status": "❌ Active future not found", "symbol": raw_symbol})

        if base_key not in signals:
            signals[base_key] = {"tf": {}, "last_action": "NONE"}

        signals[base_key]["tf"][tf] = signal
        handle_trade_decision(kite, base_key, exchange, fut_symbol, signal)

        return jsonify({"status": "✅ processed", "exchange": exchange,
                        "fut": fut_symbol, "signal": signal, "tf": tf})

    except Exception as e:
        logging.exception("webhook error")
        return jsonify({"status": "❌ error", "error": str(e)})

# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------
if __name__ == "__main__":
    print("📅 Start Time:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("📂 Directory:", os.getcwd())
    print("✅ Flask running in NATGASMINI-Only mode with 4-day rollover at http://0.0.0.0:5000/webhook")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
