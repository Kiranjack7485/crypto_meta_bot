import os
import time
import pytz
from datetime import datetime, timedelta, time as dt_time
from binance.client import Client
from binance.exceptions import BinanceAPIException
import pandas as pd
import requests
from dotenv import load_dotenv
from math import floor
from decimal import Decimal, ROUND_DOWN

# ===== FORCE LOAD ENV FIRST =====
load_dotenv(override=True)

# ===== TESTNET / LIVE SWITCH =====
BINANCE_TESTNET = True  # SET TO True FOR DEMO, False FOR LIVE
# =================================

SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
CAPITAL_USAGE_PCT = 0.90
MAX_LEVERAGE = 10
MIN_LEVERAGE = 2
RISK_PER_TRADE_PCT = 0.02

ENTRY_TF = '5m'
CONTEXT_TF = '15m'
BIAS_TF = '1h'
MAX_HOLD_MINUTES = 15

# US/LONDON OVERLAP: 6:00PM - 11:30PM IST
TRADING_START = dt_time(18, 0)
TRADING_END = dt_time(23, 30)

IST_TIMEZONE = pytz.timezone('Asia/Kolkata')
REPORT_TIME = dt_time(23, 30)

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

if BINANCE_TESTNET:
    API_KEY = os.getenv('BINANCE_TESTNET_API_KEY')
    API_SECRET = os.getenv('BINANCE_TESTNET_API_SECRET')
else:
    API_KEY = os.getenv('BINANCE_API_KEY')
    API_SECRET = os.getenv('BINANCE_API_SECRET')

if not API_KEY or not API_SECRET or len(API_KEY) < 50:
    raise Exception(f"❌ {'TESTNET' if BINANCE_TESTNET else 'LIVE'} API keys missing/invalid in .env file")
if not BOT_TOKEN or not CHAT_ID:
    raise Exception("❌ TELEGRAM credentials missing in .env file")

print(f"Mode: {'TESTNET' if BINANCE_TESTNET else 'LIVE'}")
print(f"Loaded API Key: {API_KEY[:6]}...{API_KEY[-4:]}")

client = Client(API_KEY, API_SECRET, testnet=BINANCE_TESTNET)
if BINANCE_TESTNET:
    client.API_URL = 'https://testnet.binancefuture.com'
else:
    client.API_URL = 'https://fapi.binance.com'

# ===== STATE =====
active_trade = None
daily_stats = {'date': datetime.now(IST_TIMEZONE).date(), 'trades': [], 'start_balance': 0, 'last_known_balance': 0}
last_signal = {sym: 0 for sym in SYMBOLS}
last_heartbeat = 0
report_sent_today = False

def send_tg(msg, silent=False):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                     json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown",
                           "disable_notification": silent}, timeout=10)
    except Exception as e:
        print(f"TG Error: {e}")

def is_trading_hours():
    now_ist = datetime.now(IST_TIMEZONE).time()
    return TRADING_START <= now_ist <= TRADING_END

def get_futures_balance():
    global daily_stats
    for attempt in range(3):
        try:
            balances = client.futures_account_balance()
            usdt = next(b for b in balances if b['asset'] == 'USDT')
            bal = float(usdt['availableBalance'])
            daily_stats['last_known_balance'] = bal
            return bal
        except Exception as e:
            if attempt == 2:
                last_bal = daily_stats.get('last_known_balance', 0)
                send_tg(f"⚠ *Balance Fetch Failed*\n`{str(e)[:80]}`\nUsing cached: `${last_bal:.2f}`")
                return last_bal
            time.sleep(2)
    return 0

def get_symbol_precision(symbol):
    # Returns (step_size, decimal_places)
    if symbol in ['BTCUSDT', 'ETHUSDT']: 
        return Decimal('0.001'), 3
    elif symbol == 'SOLUSDT': 
        return Decimal('0.01'), 2
    else: 
        return Decimal('0.001'), 3

def set_leverage(symbol, leverage):
    try:
        client.futures_change_leverage(symbol=symbol, leverage=leverage)
        return True
    except Exception as e:
        send_tg(f"⚠ *Leverage Error {symbol}*\n`{str(e)[:100]}`")
        return False

def get_position(symbol):
    try:
        positions = client.futures_position_information(symbol=symbol)
        pos = positions[0]
        amt = float(pos['positionAmt'])
        if amt!= 0:
            return {'symbol': symbol, 'amt': amt, 'entry': float(pos['entryPrice']),
                    'side': 'LONG' if amt > 0 else 'SHORT', 'unrealizedPnl': float(pos['unRealizedProfit'])}
        return None
    except: return None

def place_futures_order(symbol, side, quantity, entry, sl, tp1, tp2):
    try:
        order = client.futures_create_order(symbol=symbol, side='BUY' if side == 'LONG' else 'SELL',
                                            type='MARKET', quantity=quantity)
        time.sleep(2)
        pos = get_position(symbol)
        if not pos: raise Exception("Position not found after market order")
        actual_entry = pos['entry']
        sl_side = 'SELL' if side == 'LONG' else 'BUY'
        client.futures_create_order(symbol=symbol, side=sl_side, type='STOP_MARKET',
                                    stopPrice=round(sl, 2), closePosition=True, timeInForce='GTC')
        client.futures_create_order(symbol=symbol, side=sl_side, type='TAKE_PROFIT_MARKET',
                                    stopPrice=round(tp1, 2), closePosition=True, timeInForce='GTC')
        return {'status': 'SUCCESS', 'entry': actual_entry, 'order_id': order['orderId']}
    except Exception as e:
        send_tg(f"💥 *ORDER FAILED {symbol}*\n`{str(e)[:200]}`")
        return {'status': 'FAILED', 'error': str(e)}

def close_position(symbol):
    try:
        pos = get_position(symbol)
        if not pos: return True
        side = 'SELL' if pos['side'] == 'LONG' else 'BUY'
        qty = abs(pos['amt'])
        client.futures_create_order(symbol=symbol, side=side, type='MARKET', quantity=qty, reduceOnly=True)
        return True
    except Exception as e:
        send_tg(f"⚠ *Close Error {symbol}*\n`{str(e)[:100]}`")
        return False

def cancel_all_open_orders(symbol):
    try: client.futures_cancel_all_open_orders(symbol=symbol)
    except: pass

def get_swings(df, lookback=5):
    highs, lows = df['h'].values, df['l'].values
    sh, sl = [], []
    for i in range(lookback, len(df) - lookback):
        if all(highs[i] > highs[i-j] for j in range(1, lookback+1)) and \
           all(highs[i] > highs[i+j] for j in range(1, lookback+1)):
            sh.append({'price': highs[i], 'idx': i, 'ts': df.index[i]})
        if all(lows[i] < lows[i-j] for j in range(1, lookback+1)) and \
           all(lows[i] < lows[i+j] for j in range(1, lookback+1)):
            sl.append({'price': lows[i], 'idx': i, 'ts': df.index[i]})
    return sh, sl

def calculate_atr(df, period=14):
    high, low, close = df['h'], df['l'], df['c']
    tr = pd.concat([high - low, abs(high - close.shift()), abs(low - close.shift())], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]

def calculate_vwap(df):
    tp = (df['h'] + df['l'] + df['c']) / 3
    return (tp * df['v']).cumsum() / df['v'].cumsum()

def calculate_cvd(df):
    buy_vol = df['v'].where(df['c'] > df['o'], 0)
    sell_vol = df['v'].where(df['c'] < df['o'], 0)
    cvd = (buy_vol - sell_vol).cumsum()
    return cvd

def get_tf_data(symbol, tf, lookback=5):
    try:
        klines = client.futures_klines(symbol=symbol, interval=tf, limit=200)
        df = pd.DataFrame(klines, columns=['ts','o','h','l','c','v','ct','qav','nt','tbbav','tbqav','ig'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        df.set_index('ts', inplace=True)
        sh, sl = get_swings(df, lookback if tf!= '5m' else 3)
        atr = calculate_atr(df)
        vwap = calculate_vwap(df)
        cvd = calculate_cvd(df)
        return {'df': df, 'sh': sh, 'sl': sl, 'atr': atr, 'price': df['c'].iloc[-1], 'vwap': vwap, 'cvd': cvd}
    except: 
        return None

def detect_sniper_entry(symbol):
    try:
        if not is_trading_hours(): return None
        if time.time() - last_signal[symbol] < 900: return None
        if active_trade: return None
        
        m5 = get_tf_data(symbol, ENTRY_TF)
        m15 = get_tf_data(symbol, CONTEXT_TF)
        h1 = get_tf_data(symbol, BIAS_TF)
        
        if not all([m5, m15, h1]) or len(h1['sh']) < 2 or len(h1['sl']) < 2: return None
        
        h1_up = h1['sh'][-1]['price'] > h1['sh'][-2]['price'] and h1['sl'][-1]['price'] > h1['sl'][-2]['price']
        h1_down = h1['sl'][-1]['price'] < h1['sl'][-2]['price'] and h1['sh'][-1]['price'] < h1['sh'][-2]['price']
        if not h1_up and not h1_down: return None
        
        df5 = m5['df']
        price_5m = m5['price']
        vwap_5m = m5['vwap'].iloc[-1]
        cvd_5m = m5['cvd']
        signal = None
        
        if h1_up and len(m5['sl']) >= 2:
            sweep_low = m5['sl'][-2]['price']
            recent_candles = df5.iloc[-5:]
            swept = (recent_candles['l'].min() < sweep_low) and (df5['c'].iloc[-1] > sweep_low)
            
            if swept:
                for i in range(len(df5)-3, len(df5)-1):
                    c1, c2, c3 = df5.iloc[i-1], df5.iloc[i], df5.iloc[i+1]
                    if c1['h'] < c3['l']:
                        fvg_mid = (c1['h'] + c3['l']) / 2
                        if abs(price_5m - fvg_mid) / price_5m < 0.001:
                            cvd_rising = cvd_5m.iloc[-1] > cvd_5m.iloc[-5]
                            vwap_support = price_5m > vwap_5m
                            m15_bullish = len(m15['sl']) >= 1 and m15['price'] > m15['sl'][-1]['price']
                            
                            if cvd_rising and vwap_support and m15_bullish:
                                sl = sweep_low - (m5['atr'] * 0.5)
                                swing_h = m5['sh'][-1]['price']
                                tp1 = swing_h
                                tp2 = swing_h + (swing_h - sweep_low) * 0.5
                                signal = {'direction': 'LONG', 'symbol': symbol, 'entry': price_5m, 
                                          'sl': sl, 'tp1': tp1, 'tp2': tp2, 'reason': 'Sweep+FVG+CVD'}
                                break
        
        elif h1_down and len(m5['sh']) >= 2:
            sweep_high = m5['sh'][-2]['price']
            recent_candles = df5.iloc[-5:]
            swept = (recent_candles['h'].max() > sweep_high) and (df5['c'].iloc[-1] < sweep_high)
            
            if swept:
                for i in range(len(df5)-3, len(df5)-1):
                    c1, c2, c3 = df5.iloc[i-1], df5.iloc[i], df5.iloc[i+1]
                    if c1['l'] > c3['h']:
                        fvg_mid = (c1['l'] + c3['h']) / 2
                        if abs(price_5m - fvg_mid) / price_5m < 0.001:
                            cvd_falling = cvd_5m.iloc[-1] < cvd_5m.iloc[-5]
                            vwap_resistance = price_5m < vwap_5m
                            m15_bearish = len(m15['sh']) >= 1 and m15['price'] < m15['sh'][-1]['price']
                            
                            if cvd_falling and vwap_resistance and m15_bearish:
                                sl = sweep_high + (m5['atr'] * 0.5)
                                swing_l = m5['sl'][-1]['price']
                                tp1 = swing_l
                                tp2 = swing_l - (sweep_high - swing_l) * 0.5
                                signal = {'direction': 'SHORT', 'symbol': symbol, 'entry': price_5m,
                                          'sl': sl, 'tp1': tp1, 'tp2': tp2, 'reason': 'Sweep+FVG+CVD'}
                                break
        
        if signal:
            last_signal[symbol] = time.time()
            return signal
        return None
    except Exception as e:
        print(f"Detection error {symbol}: {e}")
        return None

def execute_trade(signal):
    global active_trade
    symbol = signal['symbol']
    if get_position(symbol):
        send_tg(f"⚠ *Skip {symbol}*\nAlready in position")
        return False
    balance = get_futures_balance()
    if balance < 5:
        send_tg(f"❌ *Low Balance*\n`${balance}` < $5 min")
        return False
    
    capital_to_use = balance * CAPITAL_USAGE_PCT
    sl_dist_pct = abs(signal['entry'] - signal['sl']) / signal['entry']
    if sl_dist_pct == 0: return False
    
    risk_amount = balance * RISK_PER_TRADE_PCT
    raw_leverage = risk_amount / (capital_to_use * sl_dist_pct)
    leverage = min(MAX_LEVERAGE, max(MIN_LEVERAGE, int(raw_leverage)))
    
    # CRITICAL FIX: Use Decimal for exact precision
    step_size, decimals = get_symbol_precision(symbol)
    raw_qty = Decimal(str(capital_to_use * leverage / signal['entry']))
    qty = float(raw_qty.quantize(step_size, rounding=ROUND_DOWN))
    
    notional = qty * signal['entry']
    if qty == 0 or notional < 5:
        send_tg(f"❌ *{symbol} Qty too small*\nNotional: ${notional:.2f} < $5 min")
        return False
    
    margin_needed = notional / leverage
    if margin_needed > capital_to_use * 1.02:
        send_tg(f"❌ *Margin Check Failed {symbol}*\nNeed: ${margin_needed:.2f}\nHave: ${capital_to_use:.2f}")
        return False
    
    if not set_leverage(symbol, leverage): return False
        
    send_tg(f"🎯 *SNIPER ENTRY* 🎯\n`{symbol}` {signal['direction']}\nReason: `{signal['reason']}`\nEntry: `${signal['entry']:.4f}`\nQty: `{qty}` | Lev: `{leverage}x`\nMargin: `${margin_needed:.2f}` | Notional: `${notional:.2f}`")
    
    result = place_futures_order(symbol, signal['direction'], qty, signal['entry'], signal['sl'], signal['tp1'], signal['tp2'])
    if result['status'] == 'SUCCESS':
        active_trade = {'symbol': symbol, 'direction': signal['direction'], 'entry': result['entry'],
                        'sl': signal['sl'], 'tp1': signal['tp1'], 'entry_time': datetime.now(IST_TIMEZONE),
                        'qty': qty, 'leverage': leverage, 'capital_used': margin_needed}
        send_tg(f"✅ *ENTRY FILLED* ✅\n`{symbol}` @ `${result['entry']:.4f}`\nSL: `${signal['sl']:.4f}` | TP1: `${signal['tp1']:.4f}`\nHolding {MAX_HOLD_MINUTES}min max")
        return True
    return False

def monitor_active_trade():
    global active_trade
    if not active_trade: return
    symbol = active_trade['symbol']
    pos = get_position(symbol)
    if not pos:
        try:
            time.sleep(3)
            trades = client.futures_account_trades(symbol=symbol, limit=1)
            if trades:
                pnl = float(trades[0]['realizedPnl'])
                commission = float(trades[0]['commission'])
                net_pnl = pnl - commission
                daily_stats['trades'].append({'symbol': symbol, 'direction': active_trade['direction'],
                                              'pnl': net_pnl, 'success': net_pnl > 0})
                emoji = "💰" if net_pnl > 0 else "🔴"
                send_tg(f"{emoji} *TRADE CLOSED* {emoji}\n`{symbol}` {active_trade['direction']}\nEntry: `${active_trade['entry']:.4f}`\nPnL: `${net_pnl:.2f}`\n_Duration: {(datetime.now(IST_TIMEZONE) - active_trade['entry_time']).seconds//60}min_")
        except Exception as e:
            send_tg(f"⚠ *PnL Fetch Error*\n`{str(e)[:100]}`")
        cancel_all_open_orders(symbol)
        active_trade = None
        return
    held_time = datetime.now(IST_TIMEZONE) - active_trade['entry_time']
    if held_time > timedelta(minutes=MAX_HOLD_MINUTES):
        close_position(symbol)
        send_tg(f"⏰ *TIME EXIT* ⏰\n`{symbol}` closed after {MAX_HOLD_MINUTES}min")

def send_daily_closeup_report():
    global report_sent_today
    now = datetime.now(IST_TIMEZONE)
    if now.time().hour == REPORT_TIME.hour and now.time().minute >= REPORT_TIME.minute:
        if now.date() == daily_stats['date'] and not report_sent_today:
            trades = daily_stats['trades']
            total = len(trades)
            mode = "TESTNET" if BINANCE_TESTNET else "LIVE"
            
            if total == 0:
                send_tg(f"📊 *DAILY CLOSEUP {daily_stats['date']}* 📊\n\nMode: `{mode}`\nNo trades today.\n\n_Session closed._")
            else:
                wins = sum(1 for t in trades if t['success'])
                losses = total - wins
                win_rate = (wins / total) * 100
                net_pnl = sum(t['pnl'] for t in trades)
                end_balance = get_futures_balance()
                start_balance = daily_stats['start_balance']
                growth = ((end_balance - start_balance) / start_balance * 100) if start_balance > 0 else 0
                
                msg = f"📊 *DAILY CLOSEUP {daily_stats['date']}* 📊\n\n"
                msg += f"*Mode:* `{mode}`\n"
                msg += f"*Total Trades:* `{total}`\n"
                msg += f"*Successful:* `{wins}` | *Unsuccessful:* `{losses}`\n"
                msg += f"*Success Ratio:* `{win_rate:.1f}%`\n"
                msg += f"*Net P&L:* `${net_pnl:.2f}`\n"
                msg += f"*Portfolio:* `${start_balance:.2f}` → `${end_balance:.2f}`\n"
                msg += f"*Portfolio Growth:* `{growth:+.2f}%`\n\n"
                msg += f"_Trading session ended. Resetting for tomorrow._"
                send_tg(msg)
            
            report_sent_today = True
    
    if now.date() > daily_stats['date']:
        daily_stats['date'] = now.date()
        daily_stats['trades'] = []
        daily_stats['start_balance'] = get_futures_balance()
        report_sent_today = False

def main():
    global last_heartbeat, daily_stats, report_sent_today
    daily_stats['last_known_balance'] = 0
    report_sent_today = False
    
    mode_text = "🧪 *TESTNET MODE* 🧪\nPaper trading with Binance Futures Testnet."
    if not BINANCE_TESTNET:
        mode_text = "🔥 *LIVE MODE* 🔥\nReal money trading."
    
    send_tg(f"{mode_text}\nStrategy: Liquidity Sweep + FVG + CVD\nTrading: 6:00PM-11:30PM IST Only")
    
    try:
        client.futures_ping()
        print("Futures API ping OK")
    except Exception as e:
        send_tg(f"❌ *Futures Ping Failed*\n`{str(e)[:200]}`")
        return

    start_bal = get_futures_balance()
    daily_stats['start_balance'] = start_bal
    daily_stats['last_known_balance'] = start_bal
    if start_bal == 0:
        send_tg(f"❌ *CRITICAL: Balance $0*\nCheck API permissions or transfer USDT to {'Testnet' if BINANCE_TESTNET else 'Futures'} wallet")
        return
    send_tg(f"👁 *Bot Online*\nBalance: `${start_bal:.2f}`\nSymbols: {', '.join(SYMBOLS)}\nMax Hold: {MAX_HOLD_MINUTES}min\n\n_Watching for traps during killzone..._")
    
    while True:
        try:
            if time.time() - last_heartbeat > 1800:
                current_bal = get_futures_balance()
                status = "TRADING" if is_trading_hours() else "STANDBY"
                send_tg(f"💓 *Heartbeat*\nStatus: `{status}`\nBalance: `${current_bal:.2f}`\nActive: {active_trade['symbol'] if active_trade else 'None'}", silent=True)
                last_heartbeat = time.time()

            send_daily_closeup_report()
            monitor_active_trade()
            if not active_trade and is_trading_hours():
                for sym in SYMBOLS:
                    sig = detect_sniper_entry(sym)
                    if sig:
                        execute_trade(sig)
                        break
                    time.sleep(1)
            time.sleep(30)
        except KeyboardInterrupt:
            if active_trade: close_position(active_trade['symbol'])
            send_tg("🔌 *Bot Stopped Manually*")
            break
        except Exception as e:
            print(f"Main error: {e}")
            send_tg(f"💥 *Main Loop Error*\n`{str(e)[:200]}`")
            time.sleep(60)

if __name__ == "__main__":
    main()