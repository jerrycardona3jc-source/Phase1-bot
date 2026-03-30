import os
import logging
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, time
import pytz
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
# ── CONFIG ─────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DEFAULT_ACCOUNT = 10000
EST = pytz.timezone("America/New_York")
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logg
logger = logging.getLogger(__name__)
# ── SESSION ────────────────────────────────────────────────────
def get_session():
now = datetime.now(EST)
hr = now.hour
if 9 <= hr < 16: return " NY SESSION"
elif 7 <= hr < 9: return " PRE-MARKET"
elif 4 <= hr < 7: return " LONDON"
elif 0 <= hr < 4: return " ASIAN"
else: return " MARKET CLOSED"
# ── FETCH REAL DATA ────────────────────────────────────────────
def fetch_data(ticker, interval="5m"):
try:
tf_map = {"1m":"1d","2m":"1d","5m":"1d","15m":"5d","30m":"5d","1h":"5d","1d":"3mo"}
period = tf_map.get(interval, "1d")
data = yf.download(ticker, interval=interval, period=period, progress=False, auto_adj
if data.empty or len(data) < 20:
return None, "No data returned — check ticker or try 1h timeframe"
# Flatten columns if MultiIndex
if isinstance(data.columns, pd.MultiIndex):
data.columns = data.columns.get_level_values(0)
data = data.rename(columns={"Open":"o","High":"h","Low":"l","Close":"c","Volume":"v"}
data = data.dropna()
return data, None
except Exception as e:
return None, str(e)
# ── INDICATORS ─────────────────────────────────────────────────
def calc_ema(series, period):
return series.ewm(span=period, adjust=False).mean()
def calc_rsi(series, period=14):
delta = series.diff()
gain = delta.where(delta > 0, 0.0)
loss = -delta.where(delta < 0, 0.0)
avg_gain = gain.ewm(com=period-1, adjust=False).mean()
avg_loss = loss.ewm(com=period-1, adjust=False).mean()
rs = avg_gain / avg_loss.replace(0, np.nan)
return 100 - (100 / (1 + rs))
def calc_vwap(data):
tp = (data['h'] + data['l'] + data['c']) / 3
cum_vol = data['v'].cumsum()
cum_pv = (tp * data['v']).cumsum()
return cum_pv / cum_vol.replace(0, np.nan)
# ── SWING POINTS ───────────────────────────────────────────────
def find_swings(data, lb=5):
highs, lows = [], []
for i in range(lb, len(data)-lb):
if all(data['h'].iloc[i] > data['h'].iloc[j] for j in range(i-lb, i+lb+1) if j highs.append({"idx": i, "price": data['h'].iloc[i]})
if all(data['l'].iloc[i] < data['l'].iloc[j] for j in range(i-lb, i+lb+1) if j lows.append({"idx": i, "price": data['l'].iloc[i]})
return highs, lows
!= i):
!= i):
# ── PHASE 1 ENGINE ─────────────────────────────────────────────
def run_phase1(data, ticker):
n = len(data)
if n < 25:
return None
closes = data['c']
e20 = calc_ema(closes, 20)
e50 = calc_ema(closes, 50)
r14 = calc_rsi(closes, 14)
vw = calc_vwap(data)
highs, lows = find_swings(data, lb=5)
last = data.iloc[-1]
prev = data.iloc[-2]
cur_p = float(last['c'])
steps = {}
# ── STEP 1: STRUCTURE ─────────────────────────────────────
recent_h = [h for h in highs if h['idx'] >= n-30][-3:]
recent_l = [l for l in lows if l['idx'] >= n-30][-3:]
bull = bear = False
if len(recent_h) >= 2 and len(recent_l) >= 2:
bull = recent_h[-1]['price'] > recent_h[0]['price'] and recent_l[-1]['price'] > recen
bear = recent_h[-1]['price'] < recent_h[0]['price'] and recent_l[-1]['price'] < recen
if not bull and not bear:
bull = float(e20.iloc[-1]) > float(e50.iloc[-1]) and cur_p > float(e20.iloc[-1])
bear = float(e20.iloc[-1]) < float(e50.iloc[-1]) and cur_p < float(e20.iloc[-1])
direction = "bull" if bull else "bear"
steps['structure'] = {
"pass": bull or bear,
"note": "Bullish HH/HL" if bull else "Bearish LL/LH" if bear else "Sideways — no clea
}
# ── STEP 2: LIQUIDITY SWEEP ───────────────────────────────
swept = False
if direction == "bull":
for l in recent_l:
if float(last['l']) < l['price'] and float(last['c']) > l['price']:
swept = True; break
if float(prev['l']) < l['price'] and float(prev['c']) > l['price']:
swept = True; break
if not swept and n > 15:
session_low = float(data['l'].iloc[-15:-1].min())
if float(last['l']) < session_low * 1.002 and float(last['c']) > session_low:
swept = True
if not swept:
lwick = min(float(last['o']), cur_p) - float(last['l'])
body = abs(cur_p - float(last['o']))
if lwick > body * 0.8 and cur_p > float(last['o']):
swept = True
else:
for h in recent_h:
if float(last['h']) > h['price'] and float(last['c']) < h['price']:
swept = True; break
if float(prev['h']) > h['price'] and float(prev['c']) < h['price']:
swept = True; break
if not swept and n > 15:
session_high = float(data['h'].iloc[-15:-1].max())
if float(last['h']) > session_high * 0.998 and float(last['c']) < session_high:
swept = True
if not swept:
uwick = float(last['h']) - max(float(last['o']), cur_p)
body = abs(cur_p - float(last['o']))
if uwick > body * 0.8 and cur_p < float(last['o']):
swept = True
steps['liquidity'] = {
"pass": swept,
"note": "Liquidity swept — institutional trap confirmed" if swept else "No sweep yet
}
# ── STEP 3: ORDER BLOCK ───────────────────────────────────
ob = None
for j in range(n-2, max(0, n-25), -1):
pc = data.iloc[j]
bd = abs(float(pc['c']) - float(pc['o']))
avg_b = float(data['c'].iloc[max(0,j-10):j].diff().abs().mean()) or 0.01
ahead = min(j+2, n-1)
mv = abs(float(data['c'].iloc[ahead]) - float(pc['c']))
if bd < avg_b * 0.1: continue
if mv < bd * 0.5: continue
if direction == "bull" and float(pc['c']) < float(pc['o']):
ob = {"h": float(pc['h']), "l": float(pc['l']), "idx": j}; break
if direction == "bear" and float(pc['c']) > float(pc['o']):
ob = {"h": float(pc['h']), "l": float(pc['l']), "idx": j}; break
if not ob:
ri = max(0, n-12)
ob = {"h": float(data['h'].iloc[ri]), "l": float(data['l'].iloc[ri]), "idx": ri}
# Validate untested
ob_valid = True
for j in range(ob['idx']+1, n-1):
if direction == "bull" and float(data['l'].iloc[j]) < ob['l'] * 0.997:
ob_valid = False; break
if direction == "bear" and float(data['h'].iloc[j]) > ob['h'] * 1.003:
ob_valid = False; break
if not ob_valid:
ri2 = max(0, n-8)
ob = {"h": float(data['h'].iloc[ri2]), "l": float(data['l'].iloc[ri2]), "idx": ri2}
steps['order_block'] = {
"pass": True,
"note": f"OB ${ob['l']:.2f} — ${ob['h']:.2f}"
}
# ── STEP 4: DISPLACEMENT ──────────────────────────────────
body_last = abs(cur_p - float(last['o']))
avg_body = float(data['c'].diff().abs().iloc[-10:].mean()) or 0.01
disp = body_last > avg_body * 1.3
steps['displacement'] = {
"pass": disp,
"note": f"Strong: {body_last/avg_body:.1f}x avg body" if disp else "Weak candle — wai
}
# ── STEP 5: RETRACEMENT ───────────────────────────────────
in_ob = False
if direction == "bull":
in_ob = float(last['l']) <= ob['h'] and float(last['h']) >= ob['l'] and cur_p > ob['l
else:
in_ob = float(last['h']) >= ob['l'] and float(last['l']) <= ob['h'] and cur_p < ob['h
lwick2 = min(float(last['o']), cur_p) - float(last['l'])
uwick2 = float(last['h']) - max(float(last['o']), cur_p)
body2 = abs(cur_p - float(last['o'])) or 0.001
rejection = (direction == "bull" and lwick2 > body2 * 0.7) or \
(direction == "bear" and uwick2 > body2 * 0.7)
steps['retracement'] = {
"pass": in_ob or rejection,
"note": "Price in OB zone" if in_ob else "Rejection wick forming" if rejection else "
}
# ── STEP 6: INDICATORS ────────────────────────────────────
ema_ok = float(e20.iloc[-1]) > float(e50.iloc[-1]) if direction == "bull" else float(e20
vwap_ok = cur_p > float(vw.iloc[-1]) if direction == "bull" else cur_p < float(vw.iloc[-1
rsi_val = float(r14.iloc[-1])
rsi_ok = 30 <= rsi_val <= 70
ind_score = (1 if ema_ok else 0) + (1 if vwap_ok else 0) + (1 if rsi_ok else 0)
steps['indicators'] = {
"pass": ind_score >= 2,
"note": f"EMA:{'OK' if ema_ok else 'X'} VWAP:{'OK' if vwap_ok else 'X'} RSI:{rsi_val:
}
# ── CONFLUENCE SCORE ──────────────────────────────────────
score = 0
if steps['structure']['pass']: score += 2
if steps['liquidity']['pass']: score += 2
score += 2 # OB always valid
if steps['displacement']['pass']: score += 1
if steps['retracement']['pass']: score += 1
if ema_ok: score += 1
if vwap_ok: score += 1
if rsi_ok: score += 1
if cur_p > float(e20.iloc[-1]) and direction == "bull": score += 0.5
if cur_p < float(e20.iloc[-1]) and direction == "bear": score += 0.5
score = min(12, round(score))
steps['targets'] = {"pass": True, "note": "Levels calculated from OB"}
# ── LEVELS ────────────────────────────────────────────────
if direction == "bull":
entry = ob['h']
sl = ob['l'] * 0.9992
risk = max(entry - sl, entry * 0.002)
tp1, tp2, tp3 = entry+risk, entry+risk*2, entry+risk*3
else:
entry = ob['l']
sl = ob['h'] * 1.0008
risk = max(sl - entry, entry * 0.002)
tp1, tp2, tp3 = entry-risk, entry-risk*2, entry-risk*3
verdict = "A+" if score >= 10 else "TAKE" if score >= 7 else "CAUTION" if score >= 5 else
return {
"ticker": ticker.upper(),
"price": cur_p,
"direction": direction,
"verdict": verdict,
"score": score,
"entry": entry,
"sl": sl,
"tp1": tp1,
"tp2": tp2,
"tp3": tp3,
"risk": risk,
"ob": ob,
"steps": steps,
"ema_ok": ema_ok,
"vwap_ok": vwap_ok,
"rsi_val": rsi_val,
"rsi_ok": rsi_ok,
}
# ── FORMAT SIGNAL MESSAGE ──────────────────────────────────────
def format_signal(r, account=DEFAULT_ACCOUNT, interval="5m"):
isL = r['direction'] == 'bull'
p = 2 if r['price'] > 10 else 4
col = " " if r['verdict'] in ['A+','TAKE'] else " " if r['verdict'] == 'CAUTION' else
arrow = " LONG" if isL else " SHORT"
risk_pct = 0.02 if r['score'] >= 10 else 0.01 if r['score'] >= 7 else 0.005
risk_amt = account * risk_pct
shares = risk_amt / r['risk'] if r['risk'] > 0 else 0
pos_val = shares * r['entry']
rr_tp3 session = get_session()
= abs(r['tp3']-r['entry']) / r['risk'] if r['risk'] > 0 else 0
step_lines = ""
step_map = [
('structure', 'Structure'),
('liquidity', 'Liquidity Sweep'),
('order_block', 'Order Block'),
('displacement','Displacement'),
('retracement', 'Retracement'),
('indicators', 'Indicators'),
('targets', 'Targets'),
]
for key, name in step_map:
s = r['steps'].get(key, {})
icon = " " if s.get('pass') else " "
step_lines += f"{icon} {name}: {s.get('note','')}\n"
msg = f"""
{col} *PHASE 1 SIGNAL — {r['ticker']}*
{arrow} | Score: {r['score']}/12 | TF: {interval.upper()}
{session}
━━━━━━━━━━━━━━━━━━━━━
*CURRENT PRICE:* ${r['price']:.{p}f}
━━━━━━━━━━━━━━━━━━━━━
*ENTRY:* ${r['entry']:.{p}f}
*STOP LOSS:* ${r['sl']:.{p}f}
*TP1 (40%):* ${r['tp1']:.{p}f} → 1:1 R:R
*TP2 (40%):* ${r['tp2']:.{p}f} → 1:2 R:R
*TP3 (20%):* ${r['tp3']:.{p}f} → 1:{rr_tp3:.1f} R:R
━━━━━━━━━━━━━━━━━━━━━
*POSITION SIZE (${account:,} acct)*
Risk: {risk_pct*100:.1f}% = ${risk_amt:.2f}
Shares: {shares:.{4 if shares < 1 else 2}f}
Position value: ${pos_val:.2f}
━━━━━━━━━━━━━━━━━━━━━
*PHASE 1 ANALYSIS*
{step_lines}
━━━━━━━━━━━━━━━━━━━━━
*VERDICT: {r['verdict']}*
"""
if r['verdict'] == 'A+':
msg += " A+ SETUP — Full confluence. Take full size."
elif r['verdict'] == 'TAKE':
msg += " VALID TRADE — Good confluence. Standard size."
elif r['verdict'] == 'CAUTION':
msg += " CAUTION — Partial confluence. Half size or wait."
else:
msg += " SKIP — Not enough confluence. Wait for better setup."
msg += f"""
━━━━━━━━━━━━━━━━━━━━━
*INVALIDATION*
Price closes {'below' if isL else 'above'} OB {'low' if isL else 'high'} ${r['ob']['l' if
Opposing displacement candle forms
RSI hits {'70+' if isL else '30-'} before TP1
10+ candles in OB with no move
"""
return msg
# ── PARSE TICKER + TIMEFRAME ───────────────────────────────────
def parse_command(text):
parts = text.upper().strip().split()
ticker = None
interval = "5m"
tf_map = {"1M":"1m","2M":"2m","5M":"5m","15M":"15m","30M":"30m","1H":"1h","1D":"1d"}
for part in parts:
if part in tf_map:
interval = tf_map[part]
elif part not in ["ANALYZE","SIGNAL","CHECK","SPY","QQQ"] or True:
if 1 <= len(part) <= 6 and part.isalpha():
ticker = part
return ticker, interval
# ── HANDLERS ───────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
msg = """
*Phase 1 AI Trading Bot*
Your personal SMC signal analyzer
*HOW TO USE:*
Just type a ticker:
`SPY` — analyze SPY on 5m
`SPY 15M` — analyze on 15m
`BTC 1H` — analyze BTC on 1h
`NVDA 5M` — analyze NVDA on 5m
*COMMANDS:*
/start — show this message
/help — how to use
/watchlist — scan all your tickers
/account 25000 — set your account size
*TIMEFRAMES:*
1M 2M 5M 15M 30M 1H 1D
*Powered by Phase 1 SMC System*
7-step confluence scoring
Real live market data
Kelly position sizing
"""
await update.message.reply_text(msg, parse_mode='Markdown')
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
msg = """
*Phase 1 Bot Help*
*ANALYZE A TICKER:*
`SPY` → full Phase 1 analysis on 5m
`SPY 1H` → analysis on 1 hour
`BTC 15M` → BTC on 15 min
`TSLA 5M` → Tesla on 5 min
*SCORING SYSTEM:*
A+ (10-12) → Take full size (2% risk)
TAKE (7-9) → Standard size (1% risk)
CAUTION (5-6) → Half size (0.5% risk)
SKIP (0-4) → Do not trade
*PHASE 1 STEPS:*
Structure (HH/HL vs LL/LH)
Liquidity Sweep
Order Block
Displacement
Retracement into OB
Indicators (EMA/VWAP/RSI)
Targets (Entry/SL/TP1/2/3)
*SET ACCOUNT SIZE:*
`/account 25000` → sets $25k account
"""
await update.message.reply_text(msg, parse_mode='Markdown')
async def set_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
try:
amount = float(context.args[0].replace('$','').replace(',',''))
context.user_data['account'] = amount
await update.message.reply_text(f" Account size set to ${amount:,.2f}")
except:
await update.message.reply_text("Usage: /account 25000")
async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
tickers = ["SPY","QQQ","AAPL","TSLA","NVDA","AMD","MSFT","AMZN"]
await update.message.reply_text(" results = []
Scanning watchlist... this takes ~30 seconds")
account = context.user_data.get('account', DEFAULT_ACCOUNT)
for t in tickers:
data, err = fetch_data(t, "5m")
if data is not None:
r = run_phase1(data, t)
if r and r['verdict'] in ['A+', 'TAKE']:
results.append(f"{' ' if r['verdict']=='A+' else ' '} *{t}* — {r['verdict']
if results:
msg = " *WATCHLIST SIGNALS*\n\n" + "\n".join(results) + "\n\nType ticker name for f
else:
msg = " *WATCHLIST SCAN COMPLETE*\n\nNo A+ or TAKE setups right now.\nMarket await update.message.reply_text(msg, parse_mode='Markdown')
may be
async def analyze_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
text = update.message.text.strip()
if not text: return
ticker, interval = parse_command(text)
if not ticker or len(ticker) < 1 or len(ticker) > 6:
await update.message.reply_text(" return
Type a ticker to analyze. Example: SPY or BTC 5M"
account = context.user_data.get('account', DEFAULT_ACCOUNT)
await update.message.reply_text(f" Analyzing *{ticker}* on {interval.upper()}...", pars
data, err = fetch_data(ticker, interval)
if data is None:
await update.message.reply_text(f" return
Error: {err}\n\nTry:\n• Different ticker\n• Diff
result = run_phase1(data, ticker)
if not result:
await update.message.reply_text(" return
Not enough data — try 1H timeframe")
msg = format_signal(result, account=account, interval=interval)
await update.message.reply_text(msg, parse_mode='Markdown')
# ── MAIN ───────────────────────────────────────────────────────
def main():
app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_cmd))
app.add_handler(CommandHandler("account", set_account))
app.add_handler(CommandHandler("watchlist", watchlist))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_message))
logger.info("Phase 1 Bot starting...")
app.run_polling(allowed_updates=Update.ALL_TYPES)
if __name__ == "__main__":
main()
