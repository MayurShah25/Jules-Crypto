import requests
import pandas as pd
import json
import os
import numpy as np
import pandas_ta as ta
from datetime import datetime
import hmac
import hashlib
import base64
import pytz

# ==========================================
# COINDCX PRO FUTURES GRID BOT (BI-DIRECTIONAL)
# ==========================================
DRY_RUN = True # Set to False to execute real USDT futures trades
SYMBOL = 'B-BTC_USDT'
TIMEFRAME = '1m'
LEVERAGE = 3.0

# FUTURES FEE STRUCTURE (Significantly lower than Spot, no TDS on derivatives usually)
FEE_RATE = 0.0005  # CoinDCX Futures Taker Fee (~0.05%)

# GRID LOGIC
GRID_INTERVAL_PCT = 0.025   # Buy/Short every 2.5% deviation to survive strong trends
MARGIN_RISK_PER_GRID = 50.0 # Spend exactly $50 USDT Margin per grid (position size will be 50 * 3 = $150)
MAX_POSITION_SIZE_USDT = 500.0
TRAILING_DISTANCE_PCT = 0.01 # Wait for 1% profit bounce
STOP_LOSS_PCT = 0.05 # Close position if it goes 5% against us (15% margin loss at 3x)

MAX_HOLD_TIME_MINUTES = 1440 # Relaxed to 24h due to wider grid

STATE_FILE = 'coindcx_state.json'
API_KEY = 'YOUR_COINDCX_API_KEY'
SECRET_KEY = 'YOUR_COINDCX_SECRET_KEY'

# TELEGRAM ALERTS
TELEGRAM_BOT_TOKEN = 'YOUR_TELEGRAM_BOT_TOKEN'
TELEGRAM_CHAT_ID = 'TELEGRAM_CHAT_ID'
# ==========================================

def send_telegram_message(message):
    if TELEGRAM_BOT_TOKEN == 'YOUR_TELEGRAM_BOT_TOKEN':
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Failed to send Telegram alert: {e}")

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
    # Fetching 1000 candles to allow for 200 SMA calculation
    url = f"https://public.coindcx.com/market_data/candles?pair={SYMBOL}&interval={TIMEFRAME}&limit=1000"
    try:
        response = requests.get(url)
        data = response.json()
        if not data: return None
        # CoinDCX returns: {open, high, low, close, volume, time}
        df = pd.DataFrame(data)
        df = df.rename(columns={'time': 'timestamp'})
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.sort_values('timestamp').reset_index(drop=True)

        # Calculate technical indicators
        df['sma_50'] = ta.sma(df['close'], length=50)
        df['sma_200'] = ta.sma(df['close'], length=200)
        df['rsi_14'] = ta.rsi(df['close'], length=14)

        adx = ta.adx(df['high'], df['low'], df['close'], length=14)
        if adx is not None and not adx.empty:
            df['adx_14'] = adx['ADX_14']
        else:
            df['adx_14'] = np.nan

        return df.tail(5) # Return last 5 candles with calculated indicators
    except Exception as e:
        print(f"Error fetching data from CoinDCX: {e}")
        return None

def execute_coindcx_order(side, price_per_unit, total_quantity, reason=""):
    if DRY_RUN:
        print(f"[{reason}] 🧪 DRY RUN: Would execute {side.upper()} for {total_quantity} BTC")
        return True
    
    url = "https://api.coindcx.com/exchange/v1/derivatives/futures/orders/create"
    body = {
        "side": side, # 'buy' or 'sell'
        "order_type": "market_order",
        "pair": SYMBOL, # Futures API uses 'pair'
        "total_quantity": total_quantity,
        "price": price_per_unit, # required by coindcx even for market orders sometimes for reference
        "leverage": LEVERAGE,
        "margin_type": "isolated"
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

            # Load Long Grids
            new_grids = {}
            for k, v in state.get('open_grids', {}).items():
                if isinstance(v, dict):
                    v['time'] = datetime.fromisoformat(v['time'])
                    new_grids[float(k)] = v
            state['open_grids'] = new_grids

            # Load Short Grids
            new_short_grids = {}
            for k, v in state.get('open_short_grids', {}).items():
                if isinstance(v, dict):
                    v['time'] = datetime.fromisoformat(v['time'])
                    new_short_grids[float(k)] = v
            state['open_short_grids'] = new_short_grids

            if 'open_short_grids' not in state:
                state['open_short_grids'] = {}

            return state
            
    return {
        'start_price': None,
        'current_grid_level': None, 
        'open_grids': {}, # Long grid positions
        'open_short_grids': {}, # Short grid positions
        'simulated_balance_usdt': 1000.0,
        'simulated_balance_btc': 0.0,
    }

def save_state(state):
    state_to_save = state.copy()

    # Serialize Long Grids
    serializable_grids = {}
    for k, v in state_to_save.get('open_grids', {}).items():
        v_copy = v.copy()
        v_copy['time'] = v['time'].isoformat()
        serializable_grids[str(k)] = v_copy
    state_to_save['open_grids'] = serializable_grids

    # Serialize Short Grids
    serializable_short_grids = {}
    for k, v in state_to_save.get('open_short_grids', {}).items():
        v_copy = v.copy()
        v_copy['time'] = v['time'].isoformat()
        serializable_short_grids[str(k)] = v_copy
    state_to_save['open_short_grids'] = serializable_short_grids

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
    now_ist = datetime.now(pytz.timezone('Asia/Kolkata'))
    
    # --- HEARTBEAT & FEEDBACK LOGIC ---
    if 'last_heartbeat' not in state:
        state['last_heartbeat'] = now_ist.isoformat()

    last_hb = datetime.fromisoformat(state['last_heartbeat'])

    # 1-hour heartbeat
    if (now_ist - last_hb).total_seconds() >= 3600:
        active_longs = sum(1 for v in state['open_grids'].values() if v['amount'] > 0)
        active_shorts = sum(1 for v in state.get('open_short_grids', {}).values() if v['amount'] > 0)

        unrealized_pnl = 0.0
        for level_str, data in state['open_grids'].items():
            if data['amount'] > 0:
                unrealized_pnl += (current_price - float(level_str)) * data['amount']

        for level_str, data in state.get('open_short_grids', {}).items():
            if data['amount'] > 0:
                unrealized_pnl += (float(level_str) - current_price) * data['amount']

        hb_msg = (f"❤️ [CoinDCX BOT HEARTBEAT] - {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}\n"
                  f"Price: ${current_price:,.2f}\n"
                  f"Active Longs: {active_longs} | Active Shorts: {active_shorts}\n"
                  f"Unrealized PNL: ${unrealized_pnl:,.2f}\n"
                  f"Wallet Balance: ${state['simulated_balance_usdt']:,.2f}")
        print(hb_msg)
        send_telegram_message(hb_msg)
        state['last_heartbeat'] = now_ist.isoformat()

    if state['start_price'] is None:
        state['start_price'] = current_price
        grid_lines = build_pct_grid(current_price)
        idx = np.searchsorted(grid_lines, current_price)
        state['current_grid_level'] = grid_lines[idx-1] if idx > 0 else grid_lines[0]
        save_state(state)
        print(f"Initialized CoinDCX USDT Grid. Center: ${current_price:,.2f}")
        return

    grid_lines = build_pct_grid(state['start_price'])
    current_grid = state['current_grid_level']
    
    idx = np.searchsorted(grid_lines, current_price)
    current_grid_level = grid_lines[idx-1] if idx > 0 else grid_lines[0]
    current_idx = np.where(grid_lines == current_grid_level)[0][0]
    
    target_buy_price = grid_lines[current_idx - 1] if current_idx > 0 else grid_lines[0]
    
    print(f"\n--- 🇮🇳 CoinDCX Live Grid Update ({now.strftime('%H:%M:%S')}) ---")
    print(f"Current Price: ₹{current_price:,.2f} | Target Buy Level: ₹{target_buy_price:,.2f} (-{((current_price - target_buy_price)/current_price)*100:.2f}%)")
    
    active_longs = {k: v for k, v in state['open_grids'].items() if v['amount'] > 0}
    active_shorts = {k: v for k, v in state.get('open_short_grids', {}).items() if v['amount'] > 0}

    print(f"Active Longs: {len(active_longs)} | Active Shorts: {len(active_shorts)}")

    for level, data in active_longs.items():
        time_held_mins = (now - data['time']).total_seconds() / 60.0
        if data['trailing']:
            trail_stop = data['peak'] * (1 - TRAILING_DISTANCE_PCT)
            tp_status = f"Trailing Stop Active @ ${trail_stop:,.2f} (Peak: ${data['peak']:,.2f})"
        else:
            tp_status = f"Waiting for ${(level * (1 + TRAILING_DISTANCE_PCT)):,.2f} activation..."
        print(f"  -> [LONG] Entry: ${level:,.2f} | Time Held: {time_held_mins:.1f}m | TP: {tp_status}")

    for level, data in active_shorts.items():
        time_held_mins = (now - data['time']).total_seconds() / 60.0
        if data['trailing']:
            trail_stop = data['trough'] * (1 + TRAILING_DISTANCE_PCT)
            tp_status = f"Trailing Stop Active @ ${trail_stop:,.2f} (Trough: ${data['trough']:,.2f})"
        else:
            tp_status = f"Waiting for ${(level * (1 - TRAILING_DISTANCE_PCT)):,.2f} activation..."
        print(f"  -> [SHORT] Entry: ${level:,.2f} | Time Held: {time_held_mins:.1f}m | TP: {tp_status}")
    
    long_levels_to_delete = []
    short_levels_to_delete = []
    
    # Process Exits (Take Profit & Stop Loss) - LONGS
    for level, data in state['open_grids'].items():
        if data['amount'] > 0:
            trigger_close = False
            close_reason = ""

            # 1. Stop Loss Check
            stop_loss_price = float(level) * (1 - STOP_LOSS_PCT)
            if current_price <= stop_loss_price:
                trigger_close = True
                close_reason = "STOP LOSS LONG"
                print(f"🛑 STOP LOSS HIT for Long at ${level:,.2f}! Price dropped below ${stop_loss_price:,.2f}")
                
            # 2. Take Profit Check
            elif data['trailing']:
                if current_price > data['peak']:
                    data['peak'] = current_price
                    print(f"🚀 Long Peak Price updated to ${current_price:,.2f} for order at ${level:,.2f}")

                trail_stop_price = data['peak'] * (1 - TRAILING_DISTANCE_PCT)
                if current_price <= trail_stop_price:
                    trigger_close = True
                    close_reason = "TAKE PROFIT LONG"
                    print(f"🔒 Long Trailing Stop Hit at ${trail_stop_price:,.2f}!")

            if trigger_close:
                btc_to_sell = data['amount']
                success = execute_coindcx_order('sell', current_price, btc_to_sell, close_reason)

                if success:
                    position_value = btc_to_sell * current_price
                    exchange_fee = position_value * FEE_RATE

                    margin_returned = (btc_to_sell * float(level)) / LEVERAGE
                    profit = position_value - (btc_to_sell * float(level))

                    state['simulated_balance_usdt'] += (margin_returned + profit - exchange_fee)

                    print(f"✅ Executed {close_reason}. Net Profit: ${profit - exchange_fee:.2f}")
                    long_levels_to_delete.append(level)

    # Process Exits (Take Profit & Stop Loss) - SHORTS
    for level, data in state.get('open_short_grids', {}).items():
        if data['amount'] > 0:
            trigger_close = False
            close_reason = ""

            # 1. Stop Loss Check
            stop_loss_price = float(level) * (1 + STOP_LOSS_PCT)
            if current_price >= stop_loss_price:
                trigger_close = True
                close_reason = "STOP LOSS SHORT"
                print(f"🛑 STOP LOSS HIT for Short at ${level:,.2f}! Price surged above ${stop_loss_price:,.2f}")

            # 2. Take Profit Check
            elif data['trailing']:
                if current_price < data['trough']:
                    data['trough'] = current_price
                    print(f"🚀 Short Trough Price updated to ${current_price:,.2f} for order at ${level:,.2f}")

                trail_stop_price = data['trough'] * (1 + TRAILING_DISTANCE_PCT)
                if current_price >= trail_stop_price:
                    trigger_close = True
                    close_reason = "TAKE PROFIT SHORT"
                    print(f"🔒 Short Trailing Stop Hit at ${trail_stop_price:,.2f}!")

            if trigger_close:
                btc_to_buy = data['amount']
                success = execute_coindcx_order('buy', current_price, btc_to_buy, close_reason)
                
                if success:
                    position_value = btc_to_buy * current_price
                    exchange_fee = position_value * FEE_RATE

                    margin_returned = (btc_to_buy * float(level)) / LEVERAGE
                    profit = (btc_to_buy * float(level)) - position_value

                    state['simulated_balance_usdt'] += (margin_returned + profit - exchange_fee)

                    print(f"✅ Executed {close_reason}. Net Profit: ${profit - exchange_fee:.2f}")
                    short_levels_to_delete.append(level)

    for level in long_levels_to_delete:
        del state['open_grids'][level]
    for level in short_levels_to_delete:
        del state['open_short_grids'][level]
        
    # Indicators for trend filtering
    sma_50 = df.iloc[-1].get('sma_50', np.nan)
    sma_200 = df.iloc[-1].get('sma_200', np.nan)
    rsi_14 = df.iloc[-1].get('rsi_14', np.nan)
    adx_14 = df.iloc[-1].get('adx_14', np.nan)

    # Filter Logic: Ranging market OR Uptrend OR Oversold
    is_ranging = not np.isnan(adx_14) and adx_14 < 25
    is_uptrend = not np.isnan(sma_50) and not np.isnan(sma_200) and sma_50 > sma_200 and rsi_14 < 70
    is_oversold = not np.isnan(rsi_14) and rsi_14 < 30

    market_favorable = is_ranging or is_uptrend or is_oversold

    # Determine Trend for Shorts
    is_downtrend = not np.isnan(sma_50) and not np.isnan(sma_200) and sma_50 < sma_200 and rsi_14 > 30
    is_overbought = not np.isnan(rsi_14) and rsi_14 > 70
    market_favorable_short = is_ranging or is_downtrend or is_overbought

    # Process Entries - LONG (Buying)
    lower_line = grid_lines[current_idx - 1] if current_idx > 0 else None
    if lower_line and current_price <= lower_line and float(lower_line) not in state['open_grids']:
        if not market_favorable:
            print(f"⚠️ Price hit long buy level ${lower_line:,.2f} but skipped due to unfavorable trend.")
        else:
            margin_per_grid = min(MARGIN_RISK_PER_GRID, MAX_POSITION_SIZE_USDT)
            if state['simulated_balance_usdt'] >= margin_per_grid:
                position_size_usd = margin_per_grid * LEVERAGE
                btc_to_buy = position_size_usd / lower_line

                print(f"📉 Price dropped to Grid Line ${lower_line:,.2f} -> OPENING LONG {btc_to_buy:.5f} BTC")
                success = execute_coindcx_order('buy', current_price, btc_to_buy, "OPEN LONG")
                
                if success:
                    fee = position_size_usd * FEE_RATE
                    state['simulated_balance_usdt'] -= (margin_per_grid + fee)
                    state['open_grids'][float(lower_line)] = {
                        'amount': btc_to_buy, 'time': now,
                        'trailing': False, 'peak': 0.0
                    }
                    state['current_grid_level'] = lower_line
            else:
                print("❌ Insufficient USDT margin to open long.")

    # Process Entries - SHORT (Selling)
    higher_line = grid_lines[current_idx + 1] if current_idx + 1 < len(grid_lines) else None
    if higher_line and current_price >= higher_line and float(higher_line) not in state.get('open_short_grids', {}):
        if not market_favorable_short:
            print(f"⚠️ Price hit short sell level ${higher_line:,.2f} but skipped due to unfavorable trend.")
        else:
            margin_per_grid = min(MARGIN_RISK_PER_GRID, MAX_POSITION_SIZE_USDT)
            if state['simulated_balance_usdt'] >= margin_per_grid:
                position_size_usd = margin_per_grid * LEVERAGE
                btc_to_sell = position_size_usd / higher_line

                print(f"📈 Price rose to Grid Line ${higher_line:,.2f} -> OPENING SHORT {btc_to_sell:.5f} BTC")
                success = execute_coindcx_order('sell', current_price, btc_to_sell, "OPEN SHORT")

                if success:
                    fee = position_size_usd * FEE_RATE
                    state['simulated_balance_usdt'] -= (margin_per_grid + fee)
                    state['open_short_grids'][float(higher_line)] = {
                        'amount': btc_to_sell, 'time': now,
                        'trailing': False, 'trough': float('inf')
                    }
                    state['current_grid_level'] = higher_line
            else:
                print("❌ Insufficient USDT margin to open short.")

    # Activate Trailing Profit - LONGS
    if higher_line and current_price >= higher_line:
        if float(current_grid) in state['open_grids'] and state['open_grids'][float(current_grid)]['amount'] > 0:
            if not state['open_grids'][float(current_grid)]['trailing']:
                print(f"📈 Price hit target Grid Line ${higher_line:,.2f} -> ACTIVATING LONG TRAILING STOP!")
                state['open_grids'][float(current_grid)]['trailing'] = True
                state['open_grids'][float(current_grid)]['peak'] = current_price
                state['current_grid_level'] = higher_line
        else:
            state['current_grid_level'] = higher_line

    # Activate Trailing Profit - SHORTS
    if lower_line and current_price <= lower_line:
        if float(current_grid) in state.get('open_short_grids', {}) and state['open_short_grids'][float(current_grid)]['amount'] > 0:
            if not state['open_short_grids'][float(current_grid)]['trailing']:
                print(f"📉 Price hit target Grid Line ${lower_line:,.2f} -> ACTIVATING SHORT TRAILING STOP!")
                state['open_short_grids'][float(current_grid)]['trailing'] = True
                state['open_short_grids'][float(current_grid)]['trough'] = current_price
                state['current_grid_level'] = lower_line
        else:
            state['current_grid_level'] = lower_line
            
    save_state(state)

if __name__ == "__main__":
    df = fetch_coindcx_ohlcv()
    if df is not None and not df.empty:
        check_logic(df)
    else:
        print("Failed to fetch live data from CoinDCX.")