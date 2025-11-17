from flask import Flask, render_template_string
import ccxt, requests, os
from datetime import datetime

app = Flask(__name__)

exchange = ccxt.cryptocom({
    'apiKey': os.environ['CRYPTOCOM_KEY'],
    'secret': os.environ['CRYPTOCOM_SECRET'],
    'enableRateLimit': True,
})

@app.route('/')
def dashboard():
    try:
        exchange.load_markets()
        prices = {s.split('/')[0]: d['last'] for s, d in exchange.fetch_tickers().items() if '/' in s and d.get('last')}
        bal = exchange.fetch_balance()['total']
        total = sum(float(bal.get('USDT',0) or 0), float(bal.get('USD',0) or 0))
        holdings = []
        for coin, amt in bal.items():
            if coin in ('USDT','USD'): continue
            amt = float(amt or 0)
            if amt > 0.0001 and coin in prices:
                val = amt * prices[coin]
                total += val
                holdings.append(f"{coin}: {amt:,.6f} → ${val:,.2f}")

        return render_template_string("""
        <!DOCTYPE html>
        <html><body style="background:#000;color:#0f0;font-family:Courier New;margin:30px">
        <h1>EMPIRE v33.8 — LIVE DASHBOARD</h1>
        <h2>Total Empire Value: <b>${{ total|round(2) }}</b></h2>
        <h3>Crypto.com Spot Holdings</h3>
        <pre style="font-size:18px">{{ holdings|join('\n') }}</pre>
        <br><small>Last update: {{ now }}</small>
        </body></html>
        """, total=total, holdings=holdings, now=datetime.now().strftime("%H:%M:%S"))
    except Exception as e:
        return f"<h1 style='color:red'>Error: {e}</h1>"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
