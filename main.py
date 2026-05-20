import os
import time
import pytz
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
import pandas as pd
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT']
IST_TIMEZONE = pytz.timezone('Asia/Kolkata')
TIMEFRAME = '5m'
SWING_LOOKBACK = 5

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# ===== FIX: Create resilient session and attach to Client after init =====
session = requests.Session()
retry = Retry(
    total=3,
    read=3,
    connect=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504]
)
adapter = HTTPAdapter(max_retries=retry)
session.mount('http://', adapter)
session.mount('https://', adapter)

# Init Client normally
client = Client(
    os.getenv('BINANCE_API_KEY'),
    os.getenv('BINANCE_API_SECRET'),
    requests_params={'timeout': 20}
)
# Now patch the session - this is the correct way
client.session = session

state = {sym: {'trend': 'NONE', 'last_hh': 0, 'last_hl': 0, 'last_lh': 999999, 'last_ll': 999999} for sym in SYMBOLS}
last_error_time = {sym: 0 for sym in SYMBOLS}

def send_tg(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print(msg)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                     json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def get_swings(df):
    highs = df['h'].values
    lows = df['l'].values
    swing_highs = []
    swing_lows = []
    for i in range(SWING_LOOKBACK, len(df) - SWING_LOOKBACK):
        if all(highs[i] > highs[i-j] for j in range(1, SWING_LOOKBACK+1)) and \
           all(highs[i] > highs[i+j] for j in range(1, SWING_LOOKBACK+1)):
            swing_highs.append({'idx': i, 'price': highs[i], 'time': df.index[i]})
        if all(lows[i] < lows[i-j] for j in range(1, SWING_LOOKBACK+1)) and \
           all(lows[i] < lows[i+j] for j in range(1, SWING_LOOKBACK+1)):
            swing_lows.append({'idx': i, 'price': lows[i], 'time': df.index[i]})
    return swing_highs, swing_lows

def detect_structure(symbol):
    global last_error_time
    if time.time() - last_error_time[symbol] < 300:
        return None
    try:
        klines = client.get_klines(symbol=symbol, interval=TIMEFRAME, limit=100)
        df = pd.DataFrame(klines, columns=['ts','o','h','l','c','v','ct','qav','nt','tbbav','tbqav','ig'])
        df[['o','h','l','c']] = df[['o','h','l','c']].astype(float)
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        df.set_index('ts', inplace=True)
        swing_highs, swing_lows = get_swings(df)
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return None
        hh1, hh2 = swing_highs[-1], swing_highs[-2]
        ll1, ll2 = swing_lows[-1], swing_lows[-2]
        current_price = df['c'].iloc[-1]
        s = state[symbol]
        msg = None
        if hh2['price'] > hh1['price'] and ll1['price'] > ll2['price'] and current_price > hh2['price']:
            if s['trend']!= 'UP':
                s['trend'] = 'UP'
                s['last_hh'] = hh2['price']
                s['last_hl'] = ll1['price']
                msg = f"🟢 *UPTREND START* 🟢\n`{symbol}` | {TIMEFRAME}\nBroke HH: `${hh2['price']:.4f}`\nLast HL: `${ll1['price']:.4f}`\nNow: `${current_price:.4f}`"
        elif s['trend'] == 'UP' and current_price < s['last_hl']:
            s['trend'] = 'DOWN'
            s['last_ll'] = current_price
            msg = f"🔴 *UPTREND ENDED* 🔴\n`{symbol}` | {TIMEFRAME}\nBroke HL: `${s['last_hl']:.4f}`\nNow: `${current_price:.4f}`"
        elif ll2['price'] < ll1['price'] and hh1['price'] < hh2['price'] and current_price < ll2['price']:
            if s['trend']!= 'DOWN':
                s['trend'] = 'DOWN'
                s['last_ll'] = ll2['price']
                s['last_lh'] = hh1['price']
                msg = f"🔴 *DOWNTREND START* 🔴\n`{symbol}` | {TIMEFRAME}\nBroke LL: `${ll2['price']:.4f}`\nLast LH: `${hh1['price']:.4f}`\nNow: `${current_price:.4f}`"
        elif s['trend'] == 'DOWN' and current_price > s['last_lh']:
            s['trend'] = 'UP'
            s['last_hh'] = current_price
            msg = f"🟢 *DOWNTREND ENDED* 🟢\n`{symbol}` | {TIMEFRAME}\nBroke LH: `${s['last_lh']:.4f}`\nNow: `${current_price:.4f}`"
        return msg
    except (BinanceAPIException, BinanceRequestException, requests.exceptions.RequestException) as e:
        last_error_time[symbol] = time.time()
        print(f"API error {symbol}: {str(e)[:100]}")
        if int(time.time()) % 3600 < 60:
            send_tg(f"⚠️ *API Hiccup* ⚠️\n`{symbol}` timeout. Retrying...")
        return None
    except Exception as e:
        print(f"Logic error {symbol}: {e}")
        return None

def main():
    send_tg(f"👁️ *Structure Bot Online - Stable*\nWatching: {', '.join(SYMBOLS)}\nTF: {TIMEFRAME} | Timeout: 20s + 3 retries")
    while True:
        try:
            for sym in SYMBOLS:
                alert = detect_structure(sym)
                if alert:
                    send_tg(alert)
                    time.sleep(2)
            time.sleep(60)
        except KeyboardInterrupt:
            send_tg("🔌 *Structure Bot Stopped*")
            break
        except Exception as e:
            print(f"Main loop crash: {e}")
            send_tg(f"💥 *Bot Main Loop Crash*\n`{str(e)[:200]}`\nRestarting in 60s...")
            time.sleep(60)

if __name__ == "__main__":
    main()