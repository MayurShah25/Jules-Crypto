import pandas as pd
import datetime
from unittest.mock import MagicMock
import builtins
import binance_testnet_bot
import coindcx_bot
import json
import os
import sys

def create_mock_df():
    data = []
    start_time = pd.Timestamp.now()
    price = 60000.0
    for i in range(100):
        data.append({
            'timestamp': start_time + datetime.timedelta(minutes=i),
            'open': price,
            'high': price + 100,
            'low': price - 100,
            'close': price - i * 50,
            'volume': 100
        })
    df = pd.DataFrame(data)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    return df

import pytz
class MockDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.current_time.replace(tzinfo=None)
        else:
            return cls.current_time.replace(tzinfo=pytz.UTC).astimezone(tz)

datetime.datetime = MockDatetime
binance_testnet_bot.datetime = MockDatetime

def test_binance():
    df = create_mock_df()
    if os.path.exists(binance_testnet_bot.STATE_FILE):
        os.remove(binance_testnet_bot.STATE_FILE)

    binance_testnet_bot.DRY_RUN = True
    binance_testnet_bot.startup_logged = False
    exchange = MagicMock()
    exchange.fetch_balance.return_value = {'USDT': {'free': 1000.0}}
    exchange.create_market_buy_order.return_value = {'id': 'mock_buy'}
    exchange.create_market_sell_order.return_value = {'id': 'mock_sell'}

    original_print = builtins.print
    def custom_print(*args, **kwargs):
        pass
    builtins.print = custom_print

    try:
        for i in range(1, len(df)):
            current_df = df.iloc[:i]
            MockDatetime.current_time = current_df.iloc[-1]['timestamp']
            binance_testnet_bot.check_logic(exchange, current_df)
    finally:
        builtins.print = original_print

def test_coindcx():
    df = create_mock_df()
    if os.path.exists(coindcx_bot.STATE_FILE):
        os.remove(coindcx_bot.STATE_FILE)

    coindcx_bot.DRY_RUN = True

    original_print = builtins.print
    def custom_print(*args, **kwargs):
        pass
    builtins.print = custom_print

    try:
        for i in range(1, len(df)):
            current_df = df.iloc[:i]
            MockDatetime.current_time = current_df.iloc[-1]['timestamp']
            coindcx_bot.check_logic(current_df)
    finally:
        builtins.print = original_print

if __name__ == "__main__":
    test_binance()
    test_coindcx()
