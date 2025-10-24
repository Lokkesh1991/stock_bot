print("🚀 Starting tradingview_zerodha_ver5_without_hedge — NATGASMINI-Only Mode with debug + 4-day rollover...")

from flask import Flask, request, jsonify
from kiteconnect import KiteConnect
import logging
import os
import json
import sys
from datetime import datetime
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
ROLLOVER_DAYS = 4  # rollover to next month when front-month DTE <= 4

# For symbol normalization (kept for completeness)
ALIAS_TO_CANON = {
    "NATGAS": "NATGASMINI",
    "NATGASMINI": "NATGASMINI",
    "NATURALGAS": "NATGASMINI",
}

signals = {}
lot_size_cache = {}

@app.route("/")
def home():
    return "✅ Botelyes Webhook — NATGASMINI-Only Mode with 4-day rollover + debug is Running!"

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
    if re.fullmatch(r"\d+", s):
        s = f"{s}m"
    m = re.fullmatch(r"(\d+)h", s)
    if m:
        s = f"{int(m.group(1)) * 60}m"
    if not s.endswith("m"):
        s += "m"
    if s == "1m0m":
        s = "10m"
    if s == "60m":
        return "60m"
    return s

def load_instruments(kite, exchange: str):
    """Fetch instruments with simple retries; log if empty."""
    last_err = None
    for i in range(3):
        try:
            data = kite.instruments(exchange)
            if data:
                return data
            logging.warning(f"⚠️ instruments({exchange}) returned empty (try {i+1}/3)")
        except Exception as e:
            last_err = e
            logging.warning(f"⚠️ instruments({exchange}) failed (try {i+1}/3): {e}")
        time.sleep(0.7)
    logging.error(f"❌ instruments({exchange}) failed/empty after retries: {last_err}")
    return []

def expiry_date(i):
    try:
        return datetime.strptime(i["expiry"], "%Y-%m-%d").date()
    except Exception:
        return None

def gas_futures_visible(kite):
    """Return ALL gas futures instruments visible on MCX (for debug + matching)."""
    instr = load_instruments(kite, "MCX")
    # 1) Strict target: NATGASMINI…FUT
    minis = [x for x in instr
             if x.get("instrument_type") == "FUT"
             and (x.get("tradingsymbol") or "").startswith("NATGASMINI")
             and (x.get("tradingsymbol") or "").endswith("FUT")]
    # 2) Log a helpful snapshot
    also_gas = [x.get("tradingsymbol") for x in instr
                if x.get("instrument_type") == "FUT"
                and "NATGAS" in (x.get("tradingsymbol") or "")]
    logging.info(f"🧪 MCX GAS-like FUT visible: {also_gas[:20]}")
    return minis

def sort_by_expiry(futs):
    """Input: list of instrument dicts. Output: same list sorted by expiry asc, and filtered if no expiry."""
    items = []
    for x in futs:
        ed = expiry_date(x)
        if ed:
            items.append((ed, x))
    items.sort(key=lambda t: t[0])
    return items

def find_current_and_next_natgasmini(kite):
    """Return (current_front, next_month) NATGASMINI FUTs as dicts (or None)."""
    minis = gas_futures_visible(kite)
    items = sort_by_expiry(minis)
    today = datetime.now().date()
    live = [x for x in items if x[0] >= today]
    if not live:
        # if all expired (weird edge), fallback to all
        live = items
    if not live:
        return (None, None)
    cur = live[0][1]
    nxt = live[1][1] if len(live) > 1 else None
    return cur, nxt

def days_to_expiry_for_symbol(kite, exchange: str, tradingsymbol: str):
    instr = load_instruments(kite, exchange)
    for x in instr:
        if x.get("tradingsymbol") == tradingsymbol:
            ed = expiry_date(x)
            if not ed:
                return None
            return max(0, (ed - datetime.now().date()).days)
    return None

def maybe_rollover_existing(kite, exchange: str, cur_symbol: str):
    """
    If we already hold a position in cur_symbol and it expires in <= ROLLOVER_DAYS,
    close it and reopen same side in the next-month NATGASMINI contract.
    Returns tradingsymbol to use after rollover (may be unchanged).
    """
    dte = days_to_expiry_for_symbol(kite, exchange, cur_symbol)
    if dte is None or dte > ROLLOVER_DAYS:
        return cur_symbol

    qty = get_position_quantity(kite, exchange, cur_symbol)
    if qty == 0:
        return cur_symbol  # nothing to roll

    cur, nxt = find_current_and_next_natgasmini(kite)
    if not nxt:
        logging.warning("⚠️ No next-month NATGASMINI FUT available; cannot rollover.")
        return cur_symbol

    side = "LONG" if qty > 0 else "SHORT"
    logging.info(f"🔄 Rollover: {cur_symbol} (DTE={dte}) → {nxt['tradingsymbol']} side={side}")
    exit_position(kite, exchange, cur_symbol, qty)
    enter_position(kite, exchange, nxt["tradingsymbol"], side)
    return nxt["tradingsymbol"]

# --- NATGAS detect (for alert symbol forms) ---
_LETTER_MONTH = set("FGHJKMNQUVXZ")
def looks_like_natgas_letter_year(s: str) -> bool:
    s = s.split(":", 1)[-1]
    m = re.search(r"NATGAS([A-Z])20\d{2}", s)
    return bool(m and m.group(1) in _LETTER_MONTH)

def is_natgas_continuous(s: str) -> bool:
    s = s.split(":", 1)[-1]
    return s.startswith("NATGAS1") or s.startswith("NATGASMINI1")

# --------------------------------------------------------------------
# Contract resolver with NEW-entry rollover routing
# --------------------------------------------------------------------
def get_active_contract(kite, tv_symbol_raw: str):
    raw_upper = (tv_symbol_raw or "").upper().strip()

    if is_natgas_continuous(raw_upper) or looks_like_natgas_letter_year(raw_upper) or "NATGAS" in raw_upper:
        exchange = "MCX"
        cur, nxt = find_current_and_next_natgasmini(kite)
        if not cur:
            logging.error("❌ No active MCX FUT found for NATGASMINI (check token/API or instruments fetch)")
            return None, None

        dte = days_to_expiry_for_symbol(kite, exchange, cur["tradingsymbol"])
        if dte is not None and dte <= ROLLOVER_DAYS and nxt:
            logging.info(f"📦 Routing NEW entries to next-month: {nxt['tradingsymbol']} (front DTE={dte}≤{ROLLOVER_DAYS})")
            return exchange, nxt["tradingsymbol"]

        logging.info(f"🟢 NATGAS alert mapped → {cur['tradingsymbol']}")
        return exchange, cur["tradingsymbol"]

    logging.warning(f"⚠️ Unexpected symbol (not NATGAS): {tv_symbol_raw}")
    return None, None

# --------------------------------------------------------------------
# Order helpers
# --------------------------------------------------------------------
def get_lot_size(kite, exchange: str, tradingsymbol: str) -> int:
    key = f"{exchange}:{tradingsymbol}"
    if key in lot_size_cache:
        return lot_size_cache[key]
    try:
        instr = load_instruments(kite, exchange)
        for x in instr:
            if x.get("tradingsymbol") == tradingsymbol:
                lot = int(x.get("lot_size", 1))
                lot_size_cache[key] = lot
                return lot
        logging.warning(f"⚠️ Lot size not found for {exchange}:{tradingsymbol}; default=1")
        return 1
    except Exception as e:
        logging.error(f"❌ Lot size error: {e}")
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
    # Rollover existing (if needed) before acting
    fut_symbol = maybe_rollover_existing(kite, exchange, fut_symbol)

    qty = get_position_quantity(kite, exchange, fut_symbol)
    side = "LONG" if qty > 0 else ("SHORT" if qty < 0 else "FLAT")

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

        # NATGAS-only safety
        if "NATGAS" not in raw_symbol.upper():
            logging.info(f"🚫 Ignored non-NATGAS symbol: {raw_symbol}")
            return jsonify({"status": "🚫 Ignored — not NATGASMINI", "symbol": raw_symbol})

        # Normalize signal
        if signal_in == "BUY":
            signal = "LONG"
        elif signal_in == "SELL":
            signal = "SHORT"
        else:
            signal = signal_in
        if signal not in {"LONG", "SHORT"}:
            return jsonify({"status": "⚠️ Ignored bad signal", "got": signal_in})

        # Normalize timeframe
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

        return jsonify({
            "status": "✅ processed",
            "exchange": exchange,
            "fut": fut_symbol,
            "signal": signal,
            "tf": tf
        })

    except Exception as e:
        logging.exception("webhook error")
        return jsonify({"status": "❌ error", "error": str(e)})

# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------
if __name__ == "__main__":
    print("📅 Start Time:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("📂 Directory:", os.getcwd())
    print("✅ Flask running in NATGASMINI-Only mode at http://0.0.0.0:5000/webhook")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
