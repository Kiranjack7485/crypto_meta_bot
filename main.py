import os
import time
import pytz
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
import pandas as pd
import numpy as np
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT']
IST_TIMEZONE = pytz.timezone('Asia/Kolkata')

TIMEFRAMES = {
    '4h': {'name': 'Macro Bias', 'lookback': 5},
    '1h': {'name': 'Bias Confirm', 'lookback': 5},
    '15m': {'name': 'Setup', 'lookback': 5},
    '3m': {'name': 'Entry', 'lookback': 3}
}

# Risk params - adjust these
ACCOUNT_SIZE = 1000 # USDT. Bot uses this to calc position size
RISK_PER_TRADE_PCT = 0.015 # 1.5% account risk per trade
MAX_LEVERAGE = 10 # Never exceed this even if SL is tight
MIN_LEVERAGE = 1 # If SL is wide, use 1x

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

session = requests.Session()
retry = Retry(total=3, read=3, connect=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry)
session.mount('http://', adapter)
session.mount('https://', adapter)

client = Client(
    os.getenv('BINANCE_API_KEY'),
    os.getenv('BINANCE_API_SECRET'),
    requests_params={'timeout': 20}
)
client.session = session

last_signal = {sym: {'direction': 'NONE', 'time': 0} for sym in SYMBOLS}

def send_tg(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print(msg)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                     json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def get_swings(df, lookback):
    highs = df['h'].values
    lows = df['l'].values
    swing_highs = []
    swing_lows = []
    for i in range(lookback, len(df) - lookback):
        if all(highs[i] > highs[i-j] for j in range(1, lookback+1)) and \
           all(highs[i] > highs[i+j] for j in range(1, lookback+1)):
            swing_highs.append({'price': highs[i], 'idx': i, 'time': df.index[i]})
        if all(lows[i] < lows[i-j] for j in range(1, lookback+1)) and \
           all(lows[i] < lows[i+j] for j in range(1, lookback+1)):
            swing_lows.append({'price': lows[i], 'idx': i, 'time': df.index[i]})
    return swing_highs, swing_lows

def calculate_atr(df, period=14):
    """Average True Range for volatility-based SL buffer"""
    high = df['h']
    low = df['l']
    close = df['c']
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return atr

def get_tf_structure(symbol, tf, lookback):
    try:
        klines = client.get_klines(symbol=symbol, interval=tf, limit=100)
        df = pd.DataFrame(klines, columns=['ts','o','h','l','c','v','ct','qav','nt','tbbav','tbqav','ig'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        df.set_index('ts', inplace=True)

        sh, sl = get_swings(df, lookback)
        atr = calculate_atr(df)

        if len(sh) < 2 or len(sl) < 2:
            return None

        hh1, hh2 = sh[-1], sh[-2]
        ll1, ll2 = sl[-1], sl[-2]
        price = df['c'].iloc[-1]

        # Calculate last swing range for TP projection
        last_swing_range = abs(hh1['price'] - ll1['price'])

        if hh1['price'] > hh2['price'] and ll1['price'] > ll2['price']:
            trend = 'UP'
        elif ll1['price'] < ll2['price'] and hh1['price'] < hh2['price']:
            trend = 'DOWN'
        else:
            trend = 'RANGE'

        return {
            'trend': trend, 'hh': hh1['price'], 'hl': ll1['price'],
            'lh': hh1['price'], 'll': ll1['price'], 'price': price,
            'atr': atr, 'swing_range': last_swing_range,
            'df': df # Pass df for deeper analysis if needed
        }
    except Exception as e:
        print(f"TF error {symbol} {tf}: {e}")
        return None

def calculate_trade_levels(direction, m3, m15, m1h):
    """
    Your exact logic converted to math:
    Entry: At 3m BOS level with 0.05% limit buffer
    SL: Below 15m HL/LH + 1x ATR buffer for wicks
    TP1: 1x previous swing range from entry
    TP2: 1.618x previous swing range from entry
    Leverage: Risk 1.5% of account based on SL distance
    """
    atr_15m = m15['atr']
    entry_buffer = 0.0005 # 0.05% better fill

    if direction == 'LONG':
        # Entry: Just above 3m HH that broke, with small buffer for limit order
        entry = m3['hh'] * (1 + entry_buffer)
        # SL: Below 15m HL minus ATR buffer
        sl = m15['hl'] - (atr_15m * 1.0)
        # TP based on 1H swing range projection
        tp1 = entry + m1h['swing_range'] * 1.0
        tp2 = entry + m1h['swing_range'] * 1.618
    else: # SHORT
        entry = m3['ll'] * (1 - entry_buffer)
        sl = m15['lh'] + (atr_15m * 1.0)
        tp1 = entry - m1h['swing_range'] * 1.0
        tp2 = entry - m1h['swing_range'] * 1.618

    # Leverage calculation: Risk 1.5% of account
    risk_amount = ACCOUNT_SIZE * RISK_PER_TRADE_PCT
    sl_distance_pct = abs(entry - sl) / entry
    if sl_distance_pct == 0:
        leverage = 1
    else:
        leverage = risk_amount / (ACCOUNT_SIZE * sl_distance_pct)
        leverage = max(MIN_LEVERAGE, min(round(leverage), MAX_LEVERAGE))

    # Position size in USDT
    position_size = ACCOUNT_SIZE * leverage

    return {
        'entry': round(entry, 4),
        'sl': round(sl, 4),
        'tp1': round(tp1, 4),
        'tp2': round(tp2, 4),
        'leverage': int(leverage),
        'position_size': round(position_size, 2),
        'risk_usd': round(risk_amount, 2),
        'rr_tp1': round(abs(tp1 - entry) / abs(entry - sl), 2)
    }

def check_mtf_confluence(symbol):
    try:
        if time.time() - last_signal[symbol]['time'] < 900: # 15min cooldown
            return None

        structures = {}
        for tf, config in TIMEFRAMES.items():
            s = get_tf_structure(symbol, tf, config['lookback'])
            if not s:
                return None
            structures[tf] = s
            time.sleep(0.2)

        h4, h1, m15, m3 = structures['4h'], structures['1h'], structures['15m'], structures['3m']
        signal = None

        # LONG: 4H+1H UP, 15m near HL, 3m BOS up
        if h4['trend'] == 'UP' and h1['trend'] == 'UP':
            near_hl = abs(m15['price'] - m15['hl']) / m15['hl'] < 0.008 # 0.8% zone
            m3_broke_up = m3['price'] > m3['hh']
            if near_hl and m3_broke_up and last_signal[symbol]['direction']!= 'LONG':
                levels = calculate_trade_levels('LONG', m3, m15, h1)
                signal = {'direction': 'LONG', **levels, 'context': h4, 'h1': h1, 'm15': m15, 'm3': m3}

        # SHORT: 4H+1H DOWN, 15m near LH, 3m BOS down
        elif h4['trend'] == 'DOWN' and h1['trend'] == 'DOWN':
            near_lh = abs(m15['price'] - m15['lh']) / m15['lh'] < 0.008
            m3_broke_down = m3['price'] < m3['ll']
            if near_lh and m3_broke_down and last_signal[symbol]['direction']!= 'SHORT':
                levels = calculate_trade_levels('SHORT', m3, m15, h1)
                signal = {'direction': 'SHORT', **levels, 'context': h4, 'h1': h1, 'm15': m15, 'm3': m3}

        if signal:
            last_signal[symbol] = {'direction': signal['direction'], 'time': time.time()}
            return signal
        return None
    except Exception as e:
        print(f"MTF error {symbol}: {e}")
        return None

def format_signal(symbol, sig):
    return f"""
🎯 *MTF STRUCTURE {sig['direction']}* 🎯
`{symbol}` | Risk: `${sig['risk_usd']}` ({RISK_PER_TRADE_PCT*100}%)

*Entry:* `${sig['entry']}` | Limit Order
*Stop Loss:* `${sig['sl']}` | 15m {'HL' if sig['direction']=='LONG' else 'LH'} + ATR
*TP1:* `${sig['tp1']}` | R:R `1:{sig['rr_tp1']}`
*TP2:* `${sig['tp2']}` | Swing Extension
*Leverage:* `{sig['leverage']}x` | Size: `${sig['position_size']}`

*Confluence:*
4H: {sig['context']['trend']} | HL: ${sig['context']['hl']:.2f}
1H: {sig['h1']['trend']} | HL: ${sig['h1']['hl']:.2f}
15m: Pullback to ${sig['m15']['hl'] if sig['direction']=='LONG' else sig['m15']['lh']:.2f}
3m: BOS at ${sig['m3']['hh'] if sig['direction']=='LONG' else sig['m3']['ll']:.2f}

_Time: {datetime.now(IST_TIMEZONE).strftime('%I:%M %p IST')}_
""".strip()

def main():
    send_tg(f"🎯 *MTF Pro Bot Online*\nStrategy: 4H→1H→15m→3m Structure\nRisk: {RISK_PER_TRADE_PCT*100}% per trade | Account: ${ACCOUNT_SIZE}\n\n_Entry/SL/TP/Leverage all calculated from live structure + ATR._")
    while True:
        try:
            for sym in SYMBOLS:
                sig = check_mtf_confluence(sym)
                if sig:
                    send_tg(format_signal(sym, sig))
                    time.sleep(3)
            time.sleep(60)
        except KeyboardInterrupt:
            send_tg("🔌 *MTF Bot Stopped*")
            break
        except Exception as e:
            print(f"Main error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()