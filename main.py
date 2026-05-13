import os
import time
import pytz
from datetime import datetime, timedelta
from binance.client import Client
from binance.exceptions import BinanceAPIException
import pandas as pd
import ta
import requests
from dotenv import load_dotenv
import logging

load_dotenv()

# ===== CONFIG - UPDATED FOR IST =====
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT']
LEVERAGE = 5
RISK_PER_TRADE = 0.015 # 1.5% account risk
DAILY_STOP = -0.03 # -3% stop trading
DAILY_GOAL = 0.06 # +6% quit for day

# US/London Overlap in US/Eastern time
SESSION_START_ET = 8 # 8am ET
SESSION_END_ET = 12 # 12pm ET
ET_TIMEZONE = pytz.timezone('US/Eastern') # Auto handles EST/EDT
IST_TIMEZONE = pytz.timezone('Asia/Kolkata')

SESSION_START = 8 # Keep for display only
SESSION_END = 12

SWEEP_LOOKBACK_H1 = 20
STOP_BUFFER_PCT = 0.006 # 0.6% SL from sweep wick

# ===== TELEGRAM =====
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

def send_tg(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[TG DISABLED] {msg}")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        logging.error(f"Telegram error: {e}")

# ===== EXCHANGE =====
client = Client(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET'))

# ===== STATE =====
daily_pnl = 0
trade_count = 0
active_session = False
last_session_date = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def is_trading_time():
    """Check if current IST time falls in 8am-12pm US/Eastern, accounting for DST"""
    now_ist = datetime.now(IST_TIMEZONE)
    now_et = now_ist.astimezone(ET_TIMEZONE)

    if now_et.weekday() >= 5: # Sat/Sun in US
        return False
    return SESSION_START_ET <= now_et.hour < SESSION_END_ET

def get_session_times_ist():
    """Helper to show current session times in IST for Telegram"""
    now_ist = datetime.now(IST_TIMEZONE)
    now_et = now_ist.astimezone(ET_TIMEZONE)

    # Create today's session start/end in ET, convert to IST
    session_start_et = now_et.replace(hour=SESSION_START_ET, minute=0, second=0, microsecond=0)
    session_end_et = now_et.replace(hour=SESSION_END_ET, minute=0, second=0, microsecond=0)

    start_ist = session_start_et.astimezone(IST_TIMEZONE).strftime('%I:%M %p IST')
    end_ist = session_end_et.astimezone(IST_TIMEZONE).strftime('%I:%M %p IST')
    return f"{start_ist} - {end_ist}"

def get_htf_levels(symbol):
    try:
        klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1DAY, limit=2)
        prev_day_high = float(klines[-2][2])
        prev_day_low = float(klines[-2][3])

        klines_4h = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_4HOUR, limit=SWEEP_LOOKBACK_H1)
        highs = [float(k[2]) for k in klines_4h]
        lows = [float(k[3]) for k in klines_4h]
        return {'pdh': prev_day_high, 'pdl': prev_day_low, 'h4_high': max(highs), 'h4_low': min(lows)}
    except Exception as e:
        logging.error(f"Error fetching HTF levels {symbol}: {e}")
        return None

def get_1m_data(symbol):
    try:
        klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=50)
        df = pd.DataFrame(klines, columns=['ts','o','h','l','c','v','ct','qav','nt','tbbav','tbqav','ig'])
        df[['o','h','l','c']] = df[['o','h','l','c']].astype(float)
        return df
    except Exception as e:
        logging.error(f"Error fetching 1m {symbol}: {e}")
        return None

def detect_signal(symbol, df_1m, levels):
    if df_1m is None or len(df_1m) < 3:
        return None

    last = df_1m.iloc[-1]
    prev = df_1m.iloc[-2]

    swept_high = prev['h'] > levels['pdh'] and prev['c'] < levels['pdh']
    swept_low = prev['l'] < levels['pdl'] and prev['c'] > levels['pdl']

    if not (swept_high or swept_low):
        return None

    direction = 'SHORT' if swept_high else 'LONG'

    if direction == 'SHORT' and last['c'] > levels['pdh']:
        return None
    if direction == 'LONG' and last['c'] < levels['pdl']:
        return None

    entry = last['c']

    if direction == 'SHORT':
        stop = prev['h'] * (1 + STOP_BUFFER_PCT)
        tp1 = entry * 0.99 # 1% move
        tp2 = entry * 0.98 # 2% move
    else:
        stop = prev['l'] * (1 - STOP_BUFFER_PCT)
        tp1 = entry * 1.01
        tp2 = entry * 1.02

    rr = abs(tp1 - entry) / abs(entry - stop)
    if rr < 1.5:
        return None

    return {
        'symbol': symbol,
        'direction': direction,
        'entry': round(entry, 4),
        'stop': round(stop, 4),
        'tp1': round(tp1, 4),
        'tp2': round(tp2, 4),
        'rr': round(rr, 2),
        'size_5x_pct': f"{LEVERAGE * abs(tp1/entry - 1) * 100:.1f}%"
    }

def format_signal(sig):
    return f"""
🚨 *GOLDEN SIGNAL FOUND* 🚨

*Coin:* `{sig['symbol']}`
*Trend:* `{sig['direction']}`
*Entry:* `{sig['entry']}`
*Stop Loss:* `{sig['stop']}` | Risk: {abs(sig['entry']/sig['stop']-1)*100:.2f}%
*TP1:* `{sig['tp1']}` | Gain 5x: `{sig['size_5x_pct']}`
*TP2:* `{sig['tp2']}` | Gain 5x: `{LEVERAGE * abs(sig['tp2']/sig['entry'] - 1) * 100:.1f}%`
*R:R to TP1:* `1:{sig['rr']}`

_Action:_ Take 70% at TP1, move SL to BE, run 30% to TP2.
_Daily Stats:_ Trades: {trade_count}/4 | PnL: {daily_pnl*100:.2f}%
""".strip()

def main():
    global daily_pnl, trade_count, active_session, last_session_date

    session_window = get_session_times_ist()
    send_tg(f"✅ *Scalp Bot Online - IST Adjusted*\nMonitoring: {', '.join(SYMBOLS)}\nSession: {session_window}\nAuto DST handled.")

    while True:
        try:
            now_ist = datetime.now(IST_TIMEZONE)
            now_et = now_ist.astimezone(ET_TIMEZONE)

            if is_trading_time():
                if not active_session:
                    active_session = True
                    last_session_date = now_et.date()
                    daily_pnl = 0
                    trade_count = 0
                    send_tg(f"🟢 *TRADING SESSION START* 🟢\n{now_ist.strftime('%Y-%m-%d %I:%M %p IST')}\nNY Time: {now_et.strftime('%I:%M %p ET')}\nGood hunting.")

                if daily_pnl <= DAILY_STOP:
                    send_tg(f"🛑 *DAILY STOP HIT* 🛑\nPnL: {daily_pnl*100:.2f}%\nShutting down for the day.")
                    active_session = False
                    time.sleep(3600)
                    continue
                if daily_pnl >= DAILY_GOAL:
                    send_tg(f"🏆 *DAILY GOAL HIT* 🏆\nPnL: {daily_pnl*100:.2f}%\nStop trading. See you tomorrow.")
                    active_session = False
                    time.sleep(3600)
                    continue
                if trade_count >= 4:
                    send_tg("⚠️ *MAX TRADES REACHED* ⚠️\n4 trades done. Session closed.")
                    active_session = False
                    time.sleep(3600)
                    continue

                for symbol in SYMBOLS:
                    levels = get_htf_levels(symbol)
                    if not levels:
                        continue
                    df_1m = get_1m_data(symbol)
                    sig = detect_signal(symbol, df_1m, levels)
                    if sig:
                        trade_count += 1
                        send_tg(format_signal(sig))
                        time.sleep(10)

                time.sleep(30)

            else:
                if active_session:
                    active_session = False
                    send_tg(f"🔴 *SESSION END* 🔴\n{now_ist.strftime('%I:%M %p IST')}\nFinal PnL: {daily_pnl*100:.2f}% | Trades: {trade_count}")
                time.sleep(300)

        except KeyboardInterrupt:
            send_tg("🔌 *Bot Stopped Manually*")
            break
        except Exception as e:
            logging.error(f"Main loop error: {e}")
            send_tg(f"⚠️ *BOT ERROR* ⚠️\n`{str(e)[:200]}`\nRestarting in 60s...")
            time.sleep(60)

if __name__ == "__main__":
    main()