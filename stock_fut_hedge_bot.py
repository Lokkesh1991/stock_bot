print("🚀 Starting tradingview_zerodha_ver5_without_hedge — NATGASMINI-Only Mode (simple 20th→next-month rule)")

from flask import Flask, request, jsonify
from kiteconnect import KiteConnect
import logging
import os
import json
import sys
from datetime import datetime, date
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

signals = {}
lot_size_cache = {}

MONTH3_TO_NUM = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT":10, "NOV":11, "DEC":12
}

# --------------------------------------------------------------------
# Flask root
# --------------------------------------------------------------------
@app.route("/")
def home():
    return "✅ Botelyes Webhook — NATGASMINI-Only Mode (20th→next-month) is Running!"

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
    s = s.replace("minutes","m").replace("minute","m").replace("mins","m").replace("min","m")
    s = s.replace(" ","")
    if re.fullmatch(r"\d+", s):
        s = f"{s}m"
    m = re.fullmatch(r"(\d+)h", s)
    if m:
        s = f"{int(m.group(1))*60}m"
    if not s.endswith("m"):
        s += "m"
    if s == "1m0m": s = "10m"
    if s == "60m": return "60m"
    return s

def load_instruments(kite, exchange: str):
    """Fetch instruments with a couple of retries."""
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

def parse_natgasmini_contracts(kite):
    """
    Return list of dicts with (tsym, y, m) for all MCX NATGASMINI futures, e.g.:
    NATGASMINI25NOVFUT -> y=2025, m=11
    """
    instr = load_instruments(kite, "MCX")
    futs = []
    for x in instr:
        ts = x.get("tradingsymbol", "")
        if x.get("instrument_type") == "FUT" and ts.startswith("NATGASMINI") and ts.endswith("FUT"):
            # Pattern: NATGASMINI25NOVFUT / NATGASMINI26JANFUT
            m = re.search(r"NATGASMINI(?P<yy>\d{2})(?P<mon>[A-Z]{3})FUT", ts)
            if not m:
                continue
            yy = int(m.group("yy"))
            mon3 = m.group("mon")
            mon = MONTH3_TO_NUM.get(mon3, 0)
            if mon == 0:
                continue
            year = 2000 + yy
            futs.append({"tradingsymbol": ts, "year": year, "month": mon})
    if not futs:
        logging.error("❌ No NATGASMINI FUTs parsed from instruments.")
    else:
        logging.info(f"🧪 NATGASMINI FUTs visible: {[f['tradingsymbol'] for f in futs]}")
    return futs

def choose_contract_by_day20_rule(futs):
    """
    Simple rule:
      - Day 1..20  -> choose current-month NATGASMINI
      - Day 21..31 -> choose next-month NATGASMINI
    Uses system date; selects first matching (year, month).
    Falls back to nearest future (sorted by year,month) if an exact match is missing.
    """
    today = date.today()
    target_year = today.year
    target_month = today.month

    # after 20th → target next month
    if today.day >= 21:
        if target_month == 12:
            target_month = 1
            target_year += 1
        else:
            target_month += 1

    # try exact match first
    matches = [f for f in futs if f["year"] == target_year and f["month"] == target_month]
    if matches:
        chosen = sorted(matches, key=lambda f: (f["year"], f["month"]))[0]
        logging.info(f"📌 Day-20 rule pick → {chosen['tradingsymbol']} (target {target_year}-{target_month:02d})")
        return chosen

    # fallback: pick the nearest future >= target (else earliest available)
    futs_sorted = sorted(futs, key=lambda f: (f["year"], f["month"]))
    later = [f for f in futs_sorted if (f["year"], f["month"]) >= (target_year, target_month)]
    chosen = later[0] if later else futs_sorted[0]
    logging.info(f"📌 Day-20 rule fallback pick → {chosen['tradingsymbol']} (target {target_year}-{target_month:02d})")
    return chosen

def is_natgas_symbol(raw: str) -> bool:
    s = (raw or "").upper()
    if ":" in s:
        s = s.split(":", 1)[-1]
    return "NATGAS" in s

# --------------------------------------------------------------------
# Contract resolver (simple 20th rule)
# --------------------------------------------------------------------
def get_active_contract(kite, tv_symbol_raw: str):
    if not is_natgas_symbol(tv_symbol_raw):
        logging.warning(f"⚠️ Unexpected symbol (not NATGAS*): {tv_symbol_raw}")
        return None, None

    futs = parse_natgasmini_contracts(kite)
    if not futs:
        return None, None

    chosen = choose_contract_by_day20_rule(futs)
    return "MCX", chosen["tradingsymbol"]

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

