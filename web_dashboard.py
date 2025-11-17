from flask import Flask, render_template_string
import ccxt
import os
from datetime import datetime

app = Flask(__name__)

# Crypto.com Exchange requires these EXACT keys (not CRYPTOCOM_KEY)
api_key    = os.environ.get('API_KEY')      # ← must be API_KEY
api_secret = os.environ.get('API_SECRET')  # ← must be API_SECRET

exchange = ccxt.cryptocom({
    'apiKey': api_key,
    'secret': api_secret,
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

@app.route('/')
def dashboard():
    try:
        exchange.load_markets()
        prices = {s.split('/')[0]: d['last'] for s, d in exchange.fetch_tickers().items() if '/' in s and d.get('last')}
        balance = exchange.fetch_balance()
        total = 0.0
        holdings = []

        for coin, amount in balance['total'].items():
            amount = float(amount or 0)
            if coin in ('USDT', 'USD', 'USC'):
                total += amount
                continue
            if amount > 0.0001 and coin in prices:
                value = amount * prices[coin]
                total += value
                holdings.append(f"{coin:<12} {amount:>14,.6f} → ${value:>12,.2f}")

        return render_template_string("""
        <html>
        <head>
            <title>EMPIRE LIVE</title>
            <meta http-equiv="refresh" content="15">
            <style>
                body {background:#000;color:#0f0;font-family:Courier New;margin:40px;}
                h1 {color:#f0f;}
            </style>
        </head>
        <body>
            <h1>EMPIRE v33.8 — LIVE DASHBOARD</h1>
            <h2>Total Empire Value: <b>${{ "%.2f" % total }}</b></h2>
            <h3>Crypto.com Spot Holdings (refreshes every 15s)</h3>
            <pre style="font-size:18px;">{{ holdings|join('\n') }}</pre>
            <br><small>Last update: {{ now }}</small>
        </body>
        </html>
        """, total=total, holdings=holdings, now=datetime.now().strftime("%H:%M:%S"))
    except Exception as e:
        return f"<h1 style='color:red'>Error: {e}</h1>"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
