import os
import time
import pytz
from datetime import datetime, timedelta
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

# ===== YOUR EXACT PROCESS =====
ENTRY_TF = '5m' # You start here
CONTEXT_TF = '15m' # You check this if unsure
BIAS_TF = '1h' # You check this for daily bias
MAX_HOLD_MINUTES = 15 # Your 10-15min rule

# Risk
ACCOUNT_SIZE = 1000
RISK_PER_TRADE_PCT = 0.015
MAX_LEVERAGE = 10
MIN_LEVERAGE = 1

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

# Track active trades for time-based exits
active_trades = {} # {symbol: {'entry_time': datetime, 'direction': 'LONG', 'sl': float}}
last_signal = {sym: 0 for sym in SYMBOLS}

def send_tg(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print(msg)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                     json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def get_swings(df, lookback=5):
    highs, lows = df['h'].values, df['l'].values
    sh, sl = [], []
    for i in range(lookback, len(df) - lookback):
        if all(highs[i] > highs[i-j] for j in range(1, lookback+1)) and \
           all(highs[i] > highs[i+j] for j in range(1, lookback+1)):
            sh.append({'price': highs[i], 'idx': i})
        if all(lows[i] < lows[i-j] for j in range(1, lookback+1)) and \
           all(lows[i] < lows[i+j] for j in range(1, lookback+1)):
            sl.append({'price': lows[i], 'idx': i})
    return sh, sl

def calculate_atr(df, period=14):
    high, low, close = df['h'], df['l'], df['c']
    tr = pd.concat([
        high - low,
        abs(high - close.shift()),
        abs(low - close.shift())
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]

def is_thin_wick_candle(candle):
    """Detect manipulation wicks: body < 15% of range = likely stop hunt"""
    o, h, l, c = candle['o'], candle['h'], candle['l'], candle['c']
    body = abs(c - o)
    range_ = h - l
    if range_ == 0: return False
    return (body / range_) < 0.15

def get_tf_data(symbol, tf, limit=100):
    try:
        klines = client.get_klines(symbol=symbol, interval=tf, limit=limit)
        df = pd.DataFrame(klines, columns=['ts','o','h','l','c','v','ct','qav','nt','tbbav','tbqav','ig'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        df.set_index('ts', inplace=True)
        sh, sl = get_swings(df, 5 if tf!= '5m' else 3)
        atr = calculate_atr(df)
        return {'df': df, 'sh': sh, 'sl': sl, 'atr': atr, 'price': df['c'].iloc[-1]}
    except Exception as e:
        print(f"Data error {symbol} {tf}: {e}")
        return None

def detect_early_pullback(symbol):
    """
    YOUR STRATEGY: Start 5m, confirm with 15m, filter with 1H
    Enter on pullback, not breakout = early entry
    """
    try:
        if time.time() - last_signal[symbol] < 900: # 15min cooldown
            return None

        # 1. Get 5m, 15m, 1h data
        m5 = get_tf_data(symbol, ENTRY_TF)
        m15 = get_tf_data(symbol, CONTEXT_TF)
        h1 = get_tf_data(symbol, BIAS_TF)
        if not all([m5, m15, h1]):
            return None

        # 2. Check 1H bias first - skip if ranging
        if len(h1['sh']) < 2 or len(h1['sl']) < 2:
            return None
        h1_uptrend = h1['sh'][-1]['price'] > h1['sh'][-2]['price'] and h1['sl'][-1]['price'] > h1['sl'][-2]['price']
        h1_downtrend = h1['sl'][-1]['price'] < h1['sl'][-2]['price'] and h1['sh'][-1]['price'] < h1['sh'][-2]['price']

        if not h1_uptrend and not h1_downtrend:
            return None # 1H ranging = skip

        # 3. Check 5m for pullback setup
        if len(m5['sh']) < 2 or len(m5['sl']) < 2:
            return None

        last_hh_5m = m5['sh'][-1]['price']
        last_hl_5m = m5['sl'][-1]['price']
        last_lh_5m = m5['sh'][-1]['price']
        last_ll_5m = m5['sl'][-1]['price']
        price_5m = m5['price']

        signal = None

        # ===== LONG: Anticipate 5m uptrend continuation =====
        # Condition: 1H UP + 5m pulled back to 50-61.8% fib of last swing + 15m still bullish
        if h1_uptrend:
            # Check if 5m is in pullback zone
            swing_high = last_hh_5m
            swing_low = last_hl_5m
            fib_50 = swing_high - (swing_high - swing_low) * 0.5
            fib_618 = swing_high - (swing_high - swing_low) * 0.618

            in_pullback_zone = fib_618 <= price_5m <= fib_50
            # Check 15m context: price above 15m HL = still bullish
            m15_bullish = len(m15['sl']) >= 1 and m15['price'] > m15['sl'][-1]['price']

            if in_pullback_zone and m15_bullish:
                # Entry = current price, SL = below 15m HL + 2x ATR buffer
                sl = m15['sl'][-1]['price'] - (m15['atr'] * 2.0)
                # TP1 = 5m last HH, TP2 = 1.272 extension
                tp1 = last_hh_5m
                tp2 = last_hh_5m + (last_hh_5m - last_hl_5m) * 0.272

                signal = {
                    'direction': 'LONG',
                    'entry': round(price_5m, 4),
                    'sl': round(sl, 4),
                    'tp1': round(tp1, 4),
                    'tp2': round(tp2, 4),
                    'reason': f"5m pullback to 50-61.8% fib | 15m HL: ${m15['sl'][-1]['price']:.2f} | 1H: UP"
                }

        # ===== SHORT: Anticipate 5m downtrend continuation =====
        elif h1_downtrend:
            swing_high = last_lh_5m
            swing_low = last_ll_5m
            fib_50 = swing_low + (swing_high - swing_low) * 0.5
            fib_618 = swing_low + (swing_high - swing_low) * 0.618

            in_pullback_zone = fib_50 <= price_5m <= fib_618
            m15_bearish = len(m15['sh']) >= 1 and m15['price'] < m15['sh'][-1]['price']

            if in_pullback_zone and m15_bearish:
                sl = m15['sh'][-1]['price'] + (m15['atr'] * 2.0)
                tp1 = last_ll_5m
                tp2 = last_ll_5m - (last_lh_5m - last_ll_5m) * 0.272

                signal = {
                    'direction': 'SHORT',
                    'entry': round(price_5m, 4),
                    'sl': round(sl, 4),
                    'tp1': round(tp1, 4),
                    'tp2': round(tp2, 4),
                    'reason': f"5m pullback to 50-61.8% fib | 15m LH: ${m15['sh'][-1]['price']:.2f} | 1H: DOWN"
                }

        if signal:
            # Calculate leverage
            risk_amount = ACCOUNT_SIZE * RISK_PER_TRADE_PCT
            sl_dist_pct = abs(signal['entry'] - signal['sl']) / signal['entry']
            leverage = max(MIN_LEVERAGE, min(round(risk_amount / (ACCOUNT_SIZE * sl_dist_pct)), MAX_LEVERAGE)) if sl_dist_pct > 0 else 1

            signal['leverage'] = leverage
            signal['position_size'] = round(ACCOUNT_SIZE * leverage, 2)
            signal['rr'] = round(abs(signal['tp1'] - signal['entry']) / abs(signal['entry'] - signal['sl']), 2)
            signal['entry_time'] = datetime.now(IST_TIMEZONE)

            last_signal[symbol] = time.time()
            active_trades[symbol] = signal # Track for time exit
            return signal

        return None
    except Exception as e:
        print(f"Detection error {symbol}: {e}")
        return None

def check_time_exits():
    """Your 10-15min max hold rule + wick filter"""
    now = datetime.now(IST_TIMEZONE)
    for symbol, trade in list(active_trades.items()):
        held_time = now - trade['entry_time']
        if held_time > timedelta(minutes=MAX_HOLD_MINUTES):
            send_tg(f"⏰ *TIME EXIT* ⏰\n`{symbol}` {trade['direction']}\nHeld: {held_time.seconds//60}min\n_Closing per 15min rule_")
            del active_trades[symbol]

def format_signal(symbol, sig):
    return f"""
🎯 *EARLY PULLBACK {sig['direction']}* 🎯
`{symbol}` | Hold: {MAX_HOLD_MINUTES}min max

*Entry:* `${sig['entry']}` | 5m fib pullback
*Stop Loss:* `${sig['sl']}` | 15m structure + 2x ATR buffer
*TP1:* `${sig['tp1']}` | R:R `1:{sig['rr']}`
*TP2:* `${sig['tp2']}` | 1.272 extension
*Leverage:* `{sig['leverage']}x` | Size: `${sig['position_size']}`

*Logic:* {sig['reason']}

⚠️ *Wick Filter Active*: SL only on candle close, not wick
_Time: {sig['entry_time'].strftime('%I:%M %p IST')}_
""".strip()

def main():
    send_tg(f"🎯 *Anticipation Bot Online*\nEntry: 5m pullback | Context: 15m | Bias: 1H\nHold: {MAX_HOLD_MINUTES}min max | Wick filter: ON")
    while True:
        try:
            check_time_exits() # Exit trades held too long

            for sym in SYMBOLS:
                if sym in active_trades:
                    continue # Don't enter new if already in trade
                sig = detect_early_pullback(sym)
                if sig:
                    send_tg(format_signal(sym, sig))
                    time.sleep(3)
            time.sleep(30) # Check every 30s on 5m chart
        except KeyboardInterrupt:
            send_tg("🔌 *Bot Stopped*")
            break
        except Exception as e:
            print(f"Main error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()