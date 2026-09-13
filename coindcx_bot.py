import requests
import pandas as pd
import json
import os
import numpy as np
from datetime import datetime
import hmac
import hashlib
import base64

# ==========================================
# COINDCX INR GRID BOT (SPOT TRADING)
# ==========================================
DRY_RUN = True # Set to False to execute real INR trades
SYMBOL = 'BTCINR'
TIMEFRAME = '1m'

# INDIA CRYPTO TAX & FEE STRUCTURE
FEE_RATE = 0.005  # CoinDCX Spot Taker Fee (~0.5%)
TDS_RATE = 0.01   # Mandatory 1% TDS on all Sells in India

# GRID LOGIC (Widened massively to overcome 1.5%+ total decay)
GRID_INTERVAL_PCT = 0.02   # Buy every 2% drop
MARGIN_RISK_PER_GRID = 5000.0 # Spend exactly ₹5,000 INR per grid
MAX_POSITION_SIZE_INR = 50000.0
TRAILING_DISTANCE_PCT = 0.015 # Wait for 1.5% profit bounce to overcome taxes

MAX_HOLD_TIME_MINUTES = 1440 # Relaxed to 24h due to wider grid

STATE_FILE = 'coindcx_state.json'
API_KEY = 'YOUR_COINDCX_API_KEY'
SECRET_KEY = 'YOUR_COINDCX_SECRET_KEY'
# ==========================================

def get_coindcx_headers(body=None):
    secret_bytes = bytes(SECRET_KEY, encoding='utf-8')
    timeStamp = int(round(datetime.now().timestamp() * 1000))
    if body is None:
        body = {}
    body['timestamp'] = timeStamp
    json_body = json.dumps(body, separators=(',', ':'))
    signature = hmac.new(secret_bytes, json_body.encode(), hashlib.sha256).hexdigest()
    return {
        'Content-Type': 'application/json',
        'X-AUTH-APIKEY': API_KEY,
        'X-AUTH-SIGNATURE': signature
    }, json_body

def fetch_coindcx_ohlcv():
    # CoinDCX Public API for klines
    url = f"https://public.coindcx.com/market_data/candles?pair=I-BTC_INR&interval={TIMEFRAME}"
    try:
        response = requests.get(url)
        data = response.json()
        if not data: return None
        # CoinDCX returns: {open, high, low, close, volume, time}
        df = pd.DataFrame(data)
        df = df.rename(columns={'time': 'timestamp'})
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.sort_values('timestamp').reset_index(drop=True)
        return df.tail(5) # Return last 5 candles
    except Exception as e:
        print(f"Error fetching data from CoinDCX: {e}")
        return None

def execute_coindcx_order(side, price_per_unit, total_quantity, reason=""):
    if DRY_RUN:
        print(f"[{reason}] 🧪 DRY RUN: Would execute {side.upper()} for {total_quantity} BTC")
        return True
    
    url = "https://api.coindcx.com/exchange/v1/orders/create"
    body = {
        "side": side, # 'buy' or 'sell'
        "order_type": "market_order",
        "market": SYMBOL,
        "total_quantity": total_quantity,
        "price_per_unit": price_per_unit # required by coindcx even for market orders sometimes for reference
    }
    
    headers, json_body = get_coindcx_headers(body)
    
    try:
        response = requests.post(url, data=json_body, headers=headers)
        data = response.json()
        if 'orders' in data or 'id' in data:
            print(f"[{reason}] ✅ LIVE ORDER EXECUTED! Placed {side.upper()} on CoinDCX.")
            return True
        else:
            print(f"[{reason}] ❌ LIVE ORDER FAILED: {data}")
            return False
    except Exception as e:
        print(f"[{reason}] ❌ LIVE ORDER FAILED: {e}")
        return False

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
            new_grids = {}
            for k, v in state.get('open_grids', {}).items():
                if isinstance(v, dict):
                    v['time'] = datetime.fromisoformat(v['time'])
                    new_grids[float(k)] = v
            state['open_grids'] = new_grids
            return state
            
    return {
        'start_price': None,
        'current_grid_level': None, 
        'open_grids': {},
        'simulated_balance_inr': 100000.0,
        'simulated_balance_btc': 0.0,
    }

def save_state(state):
    state_to_save = state.copy()
    serializable_grids = {}
    for k, v in state_to_save.get('open_grids', {}).items():
        v_copy = v.copy()
        v_copy['time'] = v['time'].isoformat()
        serializable_grids[str(k)] = v_copy
    state_to_save['open_grids'] = serializable_grids
    with open(STATE_FILE, 'w') as f:
        json.dump(state_to_save, f)

def build_pct_grid(start_price, num_levels=1000):
    return np.array(sorted([start_price * ((1 + GRID_INTERVAL_PCT) ** i) for i in range(-num_levels, num_levels + 1)]))

def check_logic(df):
    state = load_state()
    current_price = df.iloc[-1]['close']
    c_low = df.iloc[-1]['low']
    c_high = df.iloc[-1]['high']
    now = datetime.now()
    
    if state['start_price'] is None:
        state['start_price'] = current_price
        grid_lines = build_pct_grid(current_price)
        idx = np.searchsorted(grid_lines, current_price)
        state['current_grid_level'] = grid_lines[idx-1] if idx > 0 else grid_lines[0]
        save_state(state)
        print(f"Initialized CoinDCX INR Grid. Center: ₹{current_price:,.2f}")
        return

    grid_lines = build_pct_grid(state['start_price'])
    current_grid = state['current_grid_level']
    
    idx = np.searchsorted(grid_lines, current_price)
    current_grid_level = grid_lines[idx-1] if idx > 0 else grid_lines[0]
    current_idx = np.where(grid_lines == current_grid_level)[0][0]

    target_buy_price = grid_lines[current_idx - 1] if current_idx > 0 else grid_lines[0]
    
    print(f"\n--- 🇮🇳 CoinDCX Live Grid Update ({now.strftime('%H:%M:%S')}) ---")
    print(f"Current Price: ₹{current_price:,.2f} | Target Buy Level: ₹{target_buy_price:,.2f} (-{((current_price - target_buy_price)/current_price)*100:.2f}%)")
    
    active_grids = {k: v for k, v in state['open_grids'].items() if v['amount'] > 0}
    print(f"Active Grids Running: {len(active_grids)}")
    for level, data in active_grids.items():
        time_held_mins = (now - data['time']).total_seconds() / 60.0

        if data['trailing']:
            trail_stop = data['peak'] * (1 - TRAILING_DISTANCE_PCT)
            tp_status = f"Trailing Stop Active @ ₹{trail_stop:,.2f} (Peak: ₹{data['peak']:,.2f})"
        else:
            tp_status = f"Waiting for ₹{(level * (1 + TRAILING_DISTANCE_PCT)):,.2f} activation..."

        print(f"  -> Entry: ₹{level:,.2f} | Time Held: {time_held_mins:.1f}m | TP: {tp_status}")
    
    levels_to_delete = []
    
    # Process Exits (Take Profit)
    for level, data in state['open_grids'].items():
        if data['amount'] > 0 and data['trailing']:
            if current_price > data['peak']:
                data['peak'] = current_price
                print(f"🚀 Peak Price updated to ₹{current_price:,.2f} for order at ₹{level:,.2f}")
                
            trail_stop_price = data['peak'] * (1 - TRAILING_DISTANCE_PCT)
            if current_price <= trail_stop_price:
                print(f"🔒 Trailing Stop Hit at ₹{trail_stop_price:,.2f}!")
                btc_to_sell = data['amount']

                success = execute_coindcx_order('sell', current_price, btc_to_sell, "TAKE PROFIT")
                
                if success:
                    gross_sell_value = btc_to_sell * current_price

                    # INDIAN TAX DEDUCTIONS
                    exchange_fee = gross_sell_value * FEE_RATE
                    tds_tax = gross_sell_value * TDS_RATE
                    net_sell_value = gross_sell_value - exchange_fee - tds_tax

                    state['simulated_balance_btc'] -= btc_to_sell
                    state['simulated_balance_inr'] += net_sell_value

                    buy_cost = data['amount'] * level
                    net_profit = net_sell_value - buy_cost
                    print(f"✅ Executed TRAIL SELL. Net Profit (After TDS & Fees): ₹{net_profit:.2f}")
                    levels_to_delete.append(level)

    for level in levels_to_delete:
        del state['open_grids'][level]
        
    # Process Entries (Buying)
    lower_line = grid_lines[current_idx - 1]
    if current_price <= lower_line:
        margin_per_grid = min(MARGIN_RISK_PER_GRID, MAX_POSITION_SIZE_INR)

        if state['simulated_balance_inr'] >= margin_per_grid:
            btc_to_buy = margin_per_grid / lower_line

            # CoinDCX minimum order is usually ₹100 INR. We check for minimum dust.
            if margin_per_grid >= 100.0:
                print(f"📉 Price dropped to Grid Line ₹{lower_line:,.2f} -> BUYING {btc_to_buy:.5f} BTC")

                success = execute_coindcx_order('buy', current_price, btc_to_buy, "GRID ENTRY")
                
                if success:
                    # Deduct exchange fee on buy
                    fee = margin_per_grid * FEE_RATE
                    state['simulated_balance_inr'] -= (margin_per_grid + fee)
                    state['simulated_balance_btc'] += btc_to_buy
                    state['open_grids'][float(lower_line)] = {
                        'amount': btc_to_buy, 'time': now,
                        'trailing': False, 'peak': 0.0
                    }
                    state['current_grid_level'] = lower_line
        else:
            print("❌ Insufficient INR balance to buy.")

    # Activate Trailing Profit
    elif current_idx + 1 < len(grid_lines):
        higher_line = grid_lines[current_idx + 1]
        if current_price >= higher_line:
            if float(current_grid) in state['open_grids'] and state['open_grids'][float(current_grid)]['amount'] > 0:
                if not state['open_grids'][float(current_grid)]['trailing']:
                    print(f"📈 Price hit target Grid Line ₹{higher_line:,.2f} -> ACTIVATING TRAILING STOP!")
                    state['open_grids'][float(current_grid)]['trailing'] = True
                    state['open_grids'][float(current_grid)]['peak'] = current_price
                    state['current_grid_level'] = higher_line
            else:
                state['current_grid_level'] = higher_line
            
    save_state(state)

if __name__ == "__main__":
    df = fetch_coindcx_ohlcv()
    if df is not None and not df.empty:
        check_logic(df)
    else:
        print("Failed to fetch live data from CoinDCX.")