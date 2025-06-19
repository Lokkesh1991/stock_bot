print("\U0001F680 Starting tradingview_zerodha_ver5_without_hedge...")

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

signals = {}
lot_size_cache = {}

@app.route("/")
def home():
    return "✅ Botelyes Trading Webhook without Hedge is Running!"

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

def get_lot_size(kite, tradingsymbol):
    if tradingsymbol in lot_size_cache:
        return lot_size_cache[tradingsymbol]
    try:
        instruments = kite.instruments("NFO")
        for item in instruments:
            if item["tradingsymbol"] == tradingsymbol:
                lot_size = item["lot_size"]
                lot_size_cache[tradingsymbol] = lot_size
                return lot_size
        return 1
    except Exception as e:
        logging.error(f"❌ Error fetching lot size: {e}")
        return 1

def enter_position(kite, fut_symbol, side):
    lot_qty = get_lot_size(kite, fut_symbol)
    txn = kite.TRANSACTION_TYPE_BUY if side == "LONG" else kite.TRANSACTION_TYPE_SELL
    kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange="NFO",
        tradingsymbol=fut_symbol,
        transaction_type=txn,
        quantity=lot_qty,
        product="NRML",
        order_type="MARKET"
    )
    logging.info(f"Entered {side} for {fut_symbol}, qty={lot_qty}")
    log_data = {
        "symbol": fut_symbol,
        "direction": side,
        "entry_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "qty": lot_qty
    }
    with open(f"logs/{fut_symbol}_trades.json", "a") as f:
        f.write(json.dumps(log_data) + "\n")
    if fut_symbol not in signals:
        signals[fut_symbol] = {"5m": "", "last_action": "NONE"}

def exit_position(kite, fut_symbol, qty):
    txn = KiteConnect.TRANSACTION_TYPE_SELL if qty > 0 else KiteConnect.TRANSACTION_TYPE_BUY
    kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange="NFO",
        tradingsymbol=fut_symbol,
        transaction_type=txn,
        quantity=abs(qty),
        product="NRML",
        order_type="MARKET"
    )
    logging.info(f"Exited position for {fut_symbol}")

def handle_trade_decision(kite, symbol, signals):
    signal_5m = signals[symbol].get("5m", "")
    if signal_5m in ["LONG", "SHORT"]:
        new_signal = signal_5m
        last_action = signals[symbol].get("last_action", "NONE")
        fut_symbol = get_active_contract(symbol)
        qty = get_position_quantity(kite, fut_symbol)

        if new_signal != last_action:
            if qty != 0:
                exit_position(kite, fut_symbol, qty)
            enter_position(kite, fut_symbol, new_signal)
            signals[symbol]["last_action"] = new_signal

def get_position_quantity(kite, symbol):
    try:
        positions = kite.positions()["net"]
        for pos in positions:
            if pos["tradingsymbol"] == symbol:
                return pos["quantity"]
        return 0
    except:
        return 0

def get_active_contract(symbol):
    today = datetime.now().date()
    current_month = today.month
    current_year = today.year
    next_month_first = datetime(current_year + int(current_month == 12), (current_month % 12) + 1, 1)
    last_day = next_month_first - timedelta(days=1)
    while last_day.weekday() != 3:
        last_day -= timedelta(days=1)
    rollover_cutoff = last_day.date() - timedelta(days=4)
    if today > rollover_cutoff:
        next_month = current_month + 1 if current_month < 12 else 1
        next_year = current_year if current_month < 12 else current_year + 1
        return f"{symbol}{str(next_year)[2:]}{datetime(next_year, next_month, 1).strftime('%b').upper()}FUT"
    else:
        return f"{symbol}{str(current_year)[2:]}{datetime(current_year, current_month, 1).strftime('%b').upper()}FUT"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        raw_symbol = data.get("symbol", "")
        signal = data.get("signal", "").lower()
        timeframe_raw = data.get("timeframe", "").lower()
        timeframe = timeframe_raw.replace("minutes", "m").replace("min", "m")
        if not timeframe.endswith("m"):
            timeframe += "m"

        if signal == "buy": signal = "LONG"
        elif signal == "sell": signal = "SHORT"
        else: signal = signal.upper()

        cleaned_symbol = re.sub(r'[^A-Z]', '', raw_symbol.upper())

        if cleaned_symbol not in signals:
            signals[cleaned_symbol] = {"5m": "", "last_action": "NONE"}

        if timeframe == "5m":
            signals[cleaned_symbol]["5m"] = signal
            kite = get_kite_client()
            if kite:
                handle_trade_decision(kite, cleaned_symbol, signals)
                return jsonify({"status": "✅ processed"})
            return jsonify({"status": "❌ kite failed"})

        return jsonify({"status": "⚠️ Ignored non-5m signal"})

    except Exception as e:
        logging.error(f"Exception: {e}")
        return jsonify({"status": "❌ error", "error": str(e)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
