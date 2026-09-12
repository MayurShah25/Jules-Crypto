import ccxt
import pandas as pd
import json
import os
import numpy as np
import requests
from datetime import datetime
import pytz

# ==========================================
# TIMED-OUT GRID TRADING + TRAILING PROFIT + KILL SWITCH
# ==========================================
DRY_RUN = False # SET TO FALSE to place real orders on the Testnet
SYMBOL = 'BTC/USDT'
TIMEFRAME = '1m'
LIMIT = 2

GRID_INTERVAL_PCT = 0.0005  
LEVERAGE = 3.0
MARGIN_RISK_PCT = 0.5      
FEE_RATE = 0.0002          
MAX_HOLD_TIME_MINUTES = 120

TRAILING_DISTANCE_PCT = 0.0002 
GRID_STOP_LOSS_PCT = 0.015 

DAILY_LOSS_LIMIT_PCT = 0.15 # Stop trading if we lose 15% today

STATE_FILE = 'binance_testnet_state.json'
startup_logged = False
import logging
import csv
from io import StringIO
import os

API_KEY = '55zZJTiycSGGtzfcVVCDHzn2cqFRx1SVzpb3WAWkKLHuccRsT56ERe75awTcfWIM' # Get from testnet.binancefuture.com
SECRET_KEY = 'WyGFiNAqlEQamT7ttsN9CQuioPo4yH9AkGW2gOZ9av56mPw5L82FTtCZt29j4GXH'

# TELEGRAM ALERTS
TELEGRAM_BOT_TOKEN = '8710147171:AAGSlfHMLB04i9kuvm2h5vU_DjHo0gWMOio'
TELEGRAM_CHAT_ID = '1499793115'
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

def get_exchange():
    exchange_config = {
        'enableRateLimit': True, 
        'options': {'defaultType': 'future', 'disableFuturesSandboxWarning': True},
        'apiKey': API_KEY,
        'secret': SECRET_KEY
    }
    exchange = ccxt.binance(exchange_config)
    exchange.set_sandbox_mode(True)
    return exchange

def build_pct_grid(start_price, num_levels=1000):
    grid = []
    for i in range(-num_levels, num_levels + 1):
        price = start_price * ((1 + GRID_INTERVAL_PCT) ** i)
        grid.append(price)
    return np.array(sorted(grid))

def get_nearest_grid_level(price, grid):
    idx = np.searchsorted(grid, price)
    return grid[idx-1] if idx > 0 else grid[0]

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
            new_grids = {}
            for k, v in state.get('open_grids', {}).items():
                if isinstance(v, dict):
                    v['time'] = datetime.fromisoformat(v['time'])
                    if 'trailing_active' not in v: v['trailing_active'] = False
                    if 'peak_price' not in v: v['peak_price'] = 0.0
                    new_grids[float(k)] = v
            state['open_grids'] = new_grids
            
            if 'daily_start_balance' not in state: state['daily_start_balance'] = state.get('simulated_balance_usdt', 20.0)
            if 'last_run_day' not in state: state['last_run_day'] = datetime.now().day
            if 'kill_switch_active' not in state: state['kill_switch_active'] = False
            return state
            
    return {
        'start_price': None,
        'current_grid_level': None, 
        'open_grids': {}, 
        'simulated_balance_usdt': 100.0, 
        'simulated_balance_btc': 0.0,
        'daily_start_balance': 100.0,
        'last_run_day': datetime.now().day,
        'kill_switch_active': False
    }

def save_state(state):
    state_to_save = state.copy()
    serializable_grids = {}
    for k, v in state_to_save.get('open_grids', {}).items():
        serializable_grids[str(k)] = {
            'amount': v['amount'], 
            'time': v['time'].isoformat(),
            'trailing_active': v['trailing_active'],
            'peak_price': v['peak_price']
        }
    state_to_save['open_grids'] = serializable_grids
    with open(STATE_FILE, 'w') as f:
        json.dump(state_to_save, f)

def fetch_data(exchange):
    try:
        ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=LIMIT)
        return pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    except Exception as e:
        print(f"Data fetch error: {e}")
        return None


def log_trade_to_csv(side, amount, real_time_equity, reason, pnl_str):
    file_exists = os.path.isfile('trade_history.csv')
    with open('trade_history.csv', 'a', newline='') as csvfile:
        fieldnames = ['timestamp', 'side', 'amount', 'real_time_equity', 'reason', 'pnl']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        writer.writerow({
            'timestamp': datetime.now(pytz.timezone('Asia/Kolkata')).isoformat(),
            'side': side,
            'amount': amount,
            'real_time_equity': real_time_equity,
            'reason': reason,
            'pnl': pnl_str
        })

def execute_market_order(exchange, side, amount, real_time_equity, reason="", pnl_str=""):

    if DRY_RUN:
        print(f"[{reason}] 🧪 DRY RUN: Would execute MARKET {side.upper()} for {amount} BTC")
        return True
    
    try:
        if side == 'buy':
            order = exchange.create_market_buy_order(SYMBOL, amount)
            icon = "📉"
        elif side == 'sell':
            order = exchange.create_market_sell_order(SYMBOL, amount)
            icon = "🚀" if "PROFIT" in reason else ("🚨" if "STOP" in reason else "⏱️")
            
        msg = f"{icon} *{reason} EXECUTED*\nPair: {SYMBOL}\nAction: MARKET {side.upper()}\nAmount: {amount} BTC"
        if pnl_str: msg += f"\n{pnl_str}"
        
        msg += f"\n💰 Current Capital: ${real_time_equity:.2f}"


        log_trade_to_csv(side, amount, real_time_equity, reason, pnl_str)


        send_telegram_message(msg)
        print(f"[{reason}] ✅ LIVE ORDER EXECUTED! Placed MARKET {side.upper()} for {amount} BTC. Order ID: {order['id']}")
        return True
    except Exception as e:
        print(f"[{reason}] ❌ LIVE ORDER FAILED: {e}")
        send_telegram_message(f"❌ *ORDER FAILED*\nReason: {reason}\nError: {e}")
        return False



def check_logic(exchange, df):
    state = load_state()
    current_price = df.iloc[-1]['close']
    now = datetime.now()
    now_ist = datetime.now(pytz.timezone('Asia/Kolkata'))
    
    total_margin_locked = sum( (data['amount'] * level / LEVERAGE) for level, data in state['open_grids'].items() if data['amount'] > 0)
    total_unrealized_pnl = sum( (data['amount'] * current_price) - (data['amount'] * level) for level, data in state['open_grids'].items() if data['amount'] > 0)
    real_time_equity = state['simulated_balance_usdt'] + total_margin_locked + total_unrealized_pnl

    global startup_logged
    if not startup_logged:
        send_telegram_message(f"🚀 Bot Started!\n💰 Current Capital: ${real_time_equity:.2f}")
        startup_logged = True
    if not DRY_RUN:
        try:
            balance_data = exchange.fetch_balance()
            if 'USDT' in balance_data:
                state['simulated_balance_usdt'] = balance_data['USDT']['free']
        except Exception as e:
            print(f"Warning: Could not fetch live balance from Testnet: {e}")

    if now_ist.day != state['last_run_day']:
        daily_pnl = real_time_equity - state['daily_start_balance']
        send_telegram_message(f"🌅 New Day (IST)!\n💰 Current Capital: ${real_time_equity:.2f}\n📊 Yesterday PNL: ${daily_pnl:.2f}")
        print(f"🌅 New Day Rollover! Resetting Kill Switch and updating Daily Balance.")
        state['last_run_day'] = now_ist.day
        state['kill_switch_active'] = False
        
        total_margin_locked = sum( (data['amount'] * level / LEVERAGE) for level, data in state['open_grids'].items() if data['amount'] > 0)
        total_unrealized_pnl = sum( (data['amount'] * current_price) - (data['amount'] * level) for level, data in state['open_grids'].items() if data['amount'] > 0)
        real_time_equity = state['simulated_balance_usdt'] + total_margin_locked + total_unrealized_pnl
        
        state['daily_start_balance'] = real_time_equity
        save_state(state)
    
    if state['start_price'] is None:
        state['start_price'] = current_price
        grid_lines = build_pct_grid(current_price)
        state['current_grid_level'] = get_nearest_grid_level(current_price, grid_lines)
        save_state(state)
        print(f"Initialized Pct Grid. Center: ${current_price:.2f} | Current Level: ${state['current_grid_level']:.2f}")
        return

    grid_lines = build_pct_grid(state['start_price'])
    current_grid = state['current_grid_level']
    
    # We should use the state's current grid level to determine the indices, not the current price's nearest lower grid
    # This prevents the bot from skipping grid levels during sharp drops.
    current_idx = np.where(np.isclose(grid_lines, current_grid))[0][0]
    
    target_buy_price = grid_lines[current_idx - 1] if current_idx > 0 else grid_lines[0]
    
    print(f"\n--- 🤖 Live Grid Update ({now.strftime('%H:%M:%S')}) ---")
    print(f"Current Price: ${current_price:.2f} | Target Buy Level: ${target_buy_price:.2f} (-{((current_price - target_buy_price)/current_price)*100:.2f}%)")
    
    active_grids = {k: v for k, v in state['open_grids'].items() if v['amount'] > 0}
    print(f"Active Grids Running: {len(active_grids)}")
    for level, data in active_grids.items():
        time_held_mins = (now - data['time']).total_seconds() / 60.0
        sl_price = level * (1 - GRID_STOP_LOSS_PCT)
        
        if data['trailing_active']:
            trail_stop = data['peak_price'] * (1 - TRAILING_DISTANCE_PCT)
            tp_status = f"Trailing Stop Active @ ${trail_stop:.2f} (Peak: ${data['peak_price']:.2f})"
        else:
            tp_status = "Waiting for activation..."
            
        print(f"  -> Entry: ${level:.2f} | Time Held: {time_held_mins:.1f}m / {MAX_HOLD_TIME_MINUTES}m | SL: ${sl_price:.2f} | TP: {tp_status}")
    
    total_margin_locked = sum( (data['amount'] * level / LEVERAGE) for level, data in state['open_grids'].items() if data['amount'] > 0)
    total_unrealized_pnl = sum( (data['amount'] * current_price) - (data['amount'] * level) for level, data in state['open_grids'].items() if data['amount'] > 0)
    real_time_equity = state['simulated_balance_usdt'] + total_margin_locked + total_unrealized_pnl
    
    daily_loss_pct = (real_time_equity - state['daily_start_balance']) / state['daily_start_balance'] if state['daily_start_balance'] > 0 else 0
    
    if daily_loss_pct <= -DAILY_LOSS_LIMIT_PCT and not state['kill_switch_active']:
        print(f"🛑 KILL SWITCH ACTIVATED! Daily loss hit {daily_loss_pct*100:.1f}%. Liquidating open positions...")
        state['kill_switch_active'] = True
        
        levels_to_delete = []
        for level, data in state['open_grids'].items():
            if data['amount'] > 0:
                btc_to_sell = data['amount']
                
                sell_value_usd = btc_to_sell * current_price
                fee = sell_value_usd * FEE_RATE
                buy_value_usd = btc_to_sell * level
                gross_profit = sell_value_usd - buy_value_usd
                net_profit = gross_profit - fee - (buy_value_usd * FEE_RATE)
                
                success = execute_market_order(exchange, 'sell', btc_to_sell, real_time_equity, "KILL SWITCH", f"Estimated Net PnL: ${net_profit:.2f}")
                
                if success:
                    fee = sell_value_usd * FEE_RATE
                    state['simulated_balance_btc'] -= btc_to_sell
                    buy_value_usd = btc_to_sell * level
                    gross_profit = sell_value_usd - buy_value_usd
                    margin_returned = buy_value_usd / LEVERAGE
                    state['simulated_balance_usdt'] += (margin_returned + gross_profit - fee)
                    levels_to_delete.append(level)
                
        for level in levels_to_delete:
            del state['open_grids'][level]
            
        save_state(state)
        return
        
    if state['kill_switch_active']:
        print("⏸️ Kill Switch is ACTIVE. Bot will not trade until tomorrow.")
        return
    
    levels_to_delete = []
    
    for level, data in state['open_grids'].items():
        if data['amount'] > 0:
            sl_price = level * (1 - GRID_STOP_LOSS_PCT)
            if current_price <= sl_price:
                print(f"🚨 HARD STOP HIT! Position at {level:.2f} dropped 1.5%. FORCING MARKET SELL!")
                btc_to_sell = data['amount']
                
                sell_value_usd = btc_to_sell * sl_price
                fee = sell_value_usd * FEE_RATE
                buy_value_usd = btc_to_sell * level
                gross_profit = sell_value_usd - buy_value_usd
                net_profit = gross_profit - fee - (buy_value_usd * FEE_RATE)
                
                success = execute_market_order(exchange, 'sell', btc_to_sell, real_time_equity, "HARD STOP", f"Estimated Net Loss: ${net_profit:.2f}")
                
                if success:
                    fee = sell_value_usd * FEE_RATE
                    state['simulated_balance_btc'] -= btc_to_sell
                    buy_value_usd = btc_to_sell * level
                    gross_profit = sell_value_usd - buy_value_usd
                    net_profit = gross_profit - fee - (buy_value_usd * FEE_RATE)
                    margin_returned = buy_value_usd / LEVERAGE
                    state['simulated_balance_usdt'] += (margin_returned + gross_profit - fee)
                    levels_to_delete.append(level)
                
    for level in levels_to_delete:
        del state['open_grids'][level]
    levels_to_delete.clear()
    
    for level, data in state['open_grids'].items():
        if data['amount'] > 0 and not data['trailing_active']:
            time_held = (now - data['time']).total_seconds() / 60.0
            if time_held >= MAX_HOLD_TIME_MINUTES:
                print(f"⏱️ Position at {level:.2f} held for {time_held:.1f} mins. FORCING MARKET SELL!")
                btc_to_sell = data['amount']
                
                sell_value_usd = btc_to_sell * current_price
                fee = sell_value_usd * FEE_RATE
                buy_value_usd = btc_to_sell * level
                gross_profit = sell_value_usd - buy_value_usd
                net_profit = gross_profit - fee - (buy_value_usd * FEE_RATE)
                
                success = execute_market_order(exchange, 'sell', btc_to_sell, real_time_equity, "TIMEOUT", f"Estimated Net PnL: ${net_profit:.2f}")
                
                if success:
                    fee = sell_value_usd * FEE_RATE
                    state['simulated_balance_btc'] -= btc_to_sell
                    buy_value_usd = btc_to_sell * level
                    gross_profit = sell_value_usd - buy_value_usd
                    net_profit = gross_profit - fee - (buy_value_usd * FEE_RATE)
                    margin_returned = buy_value_usd / LEVERAGE
                    state['simulated_balance_usdt'] += (margin_returned + gross_profit - fee)
                    levels_to_delete.append(level)
                
    for level in levels_to_delete:
        del state['open_grids'][level]
    levels_to_delete.clear()
        
    for level, data in state['open_grids'].items():
        if data['amount'] > 0 and data['trailing_active']:
            if current_price > data['peak_price']:
                data['peak_price'] = current_price
                print(f"🚀 Peak Price updated to ${current_price:.2f} for order at {level:.2f}")
                
            trail_stop_price = data['peak_price'] * (1 - TRAILING_DISTANCE_PCT)
            if current_price <= trail_stop_price:
                print(f"🔒 Trailing Stop Hit at ${trail_stop_price:.2f}!")
                btc_to_sell = data['amount']
                
                sell_value_usd = btc_to_sell * current_price 
                fee = sell_value_usd * FEE_RATE
                buy_value_usd = btc_to_sell * level
                gross_profit = sell_value_usd - buy_value_usd
                net_profit = gross_profit - fee - (buy_value_usd * FEE_RATE)
                
                success = execute_market_order(exchange, 'sell', btc_to_sell, real_time_equity, "TAKE PROFIT", f"Estimated Net Profit: ${net_profit:.2f}")
                
                if success:
                    fee = sell_value_usd * FEE_RATE
                    state['simulated_balance_btc'] -= btc_to_sell
                    buy_value_usd = btc_to_sell * level
                    gross_profit = sell_value_usd - buy_value_usd
                    net_profit = gross_profit - fee - (buy_value_usd * FEE_RATE)
                    margin_returned = buy_value_usd / LEVERAGE
                    state['simulated_balance_usdt'] += (margin_returned + gross_profit - fee)
                    levels_to_delete.append(level)
                
    for level in levels_to_delete:
        del state['open_grids'][level]
        
    lower_line = grid_lines[current_idx - 1] if current_idx > 0 else grid_lines[0]
    if current_idx > 0 and current_price <= lower_line:
        margin_per_grid = state['simulated_balance_usdt'] * MARGIN_RISK_PCT
        
        if state['simulated_balance_usdt'] >= margin_per_grid:
            position_size_usd = margin_per_grid * LEVERAGE
            btc_to_buy = round(position_size_usd / lower_line, 5) 
            
            if btc_to_buy >= 0.001: 
                print(f"📉 Price dropped to Grid Line ${lower_line:.2f} -> BUYING {btc_to_buy} BTC")
                
                success = execute_market_order(exchange, 'buy', btc_to_buy, real_time_equity, "GRID ENTRY")
                
                if success:
                    fee = position_size_usd * FEE_RATE
                    state['simulated_balance_usdt'] -= (margin_per_grid + fee)
                    state['simulated_balance_btc'] += btc_to_buy
                    state['open_grids'][float(lower_line)] = {
                        'amount': btc_to_buy, 'time': now, 
                        'trailing_active': False, 'peak_price': 0.0
                    }
                    state['current_grid_level'] = lower_line
            else:
                print(f"❌ Order size too small for Binance Testnet ({btc_to_buy} BTC). Increase risk % or simulated balance.")
        else:
            print("❌ Insufficient USDT margin to buy.")
            
    elif current_idx + 1 < len(grid_lines):
        higher_line = grid_lines[current_idx + 1]
        if current_price >= higher_line:
            if float(current_grid) in state['open_grids'] and state['open_grids'][float(current_grid)]['amount'] > 0:
                if not state['open_grids'][float(current_grid)]['trailing_active']:
                    print(f"📈 Price hit target Grid Line ${higher_line:.2f} -> ACTIVATING TRAILING STOP!")
                    state['open_grids'][float(current_grid)]['trailing_active'] = True
                    state['open_grids'][float(current_grid)]['peak_price'] = current_price
                    state['current_grid_level'] = higher_line
            else:
                state['current_grid_level'] = higher_line
            
    save_state(state)

def main():
    exchange = get_exchange()
    df = fetch_data(exchange)
    if df is not None:
        check_logic(exchange, df)

if __name__ == "__main__":
    main()