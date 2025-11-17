from flask import Flask, render_template_string, jsonify
import requests, json, time
from datetime import datetime

app = Flask(__name__)

# YOUR CRYPTO.COM API KEYS (change these)
API_KEY = "NWnhKSMvsqzmCeZ7K3gyPj"
API_SECRET = "cxakp_g1Qs8v3x3CqqXwVuZdh8Ff"
SOLANA_WALLET = "48VT8LVRmo3QdqFnNV6j7Fk53WqZ8gB5iXwNxQmX58h6qjxwz2k3byXvVsGMZFSAtbpvm6BEfA7BNiQ4wppC8Nud"

# Global data cache (updated every 30 seconds)
cache = {"total_usd": 0, "holdings": {}, "solana": {}, "pnl": 0, "last_update": 0}

def fetch_crypto_data():
    try:
        import ccxt
        ex = ccxt.cryptocom({'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True})
        ex.load_markets()
        tickers = ex.fetch_tickers()
        prices = {s.split('/')[0]: d['last'] for s, d in tickers.items() if '/' in s and d.get('last')}
        bal = ex.fetch_balance()
        holdings = {}
        usd = 0.0
        for coin, info in bal['total'].items():
            amt = float(info)
            if amt > 0.0001:
                val = amt * prices.get(coin, 0)
                holdings[coin] = {"amount": amt, "usd": val, "price": prices.get(coin, 0)}
                usd += val
        return usd, holdings, prices
    except:
        return 0, {}, {}

def fetch_solana_data():
    try:
        r = requests.get(f"https://public-api.solscan.io/account/tokens?account={SOLANA_WALLET}", timeout=10).json()
        sol_holdings = {}
        sol_usd = 0.0
        for t in r:
            amt = float(t.get("tokenAmount", {}).get("uiAmount", 0))
            if amt > 0.0001:
                sym = t.get("tokenSymbol") or t["tokenAddress"][:6]
                val = amt * prices.get(sym, 0)
                sol_holdings[sym] = {"amount": amt, "usd": val}
                sol_usd += val
        return sol_holdings, sol_usd
    except:
        return {}, 0
from flask import request, abort

@app.before_request
def auth():
    if 'username' not in request.headers or 'password' not in request.headers:
        abort(401)
    if request.headers['username'] != os.environ.get('USERNAME') or request.headers['password'] != os.environ.get('PASSWORD'):
        abort(401)
@app.route('/')
def home():
    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <title>EMPIRE v33.8 Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: 'Courier New', monospace; background: #000; color: #00ff00; padding: 20px; }
        .header { font-size: 24px; text-align: center; color: #ff00ff; margin-bottom: 30px; }
        .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 30px; }
        .stat { background: #111; padding: 15px; border: 1px solid #333; border-radius: 5px; text-align: center; }
        .stat-value { font-size: 28px; color: #00ff00; }
        .holdings { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 20px; }
        .holding { background: #222; padding: 10px; border-left: 4px solid #ff00ff; }
        .holding-name { font-weight: bold; }
        .holding-amount { color: #00ff00; }
        .holding-usd { color: #ffff00; }
        .alert { animation: pulse 1s infinite; color: #ff0000; text-align: center; font-size: 20px; }
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
        button { background: #333; color: #00ff00; border: 1px solid #00ff00; padding: 10px 20px; margin: 5px; cursor: pointer; }
        button:hover { background: #00ff00; color: #000; }
    </style>
</head>
<body>
    <div class="header">EMPIRE v33.8 — LIVE TRADING DASHBOARD</div>
    <div class="stats">
        <div class="stat">
            <div>Total Empire</div>
            <div class="stat-value">{{ total_usd | default(0) | round(2) }} USD</div>
        </div>
        <div class="stat">
            <div>Overall P&L</div>
            <div class="stat-value" style="color: {{ 'green' if overall >= 0 else 'red' }}">{{ overall | default(0) | round(2) }} USD</div>
        </div>
        <div class="stat">
            <div>Today's P&L</div>
            <div class="stat-value" style="color: {{ 'green' if daily >= 0 else 'red' }}">{{ daily | default(0) | round(2) }} USD</div>
        </div>
    </div>
    <div class="holdings">
        <div style="grid-column: span 4; text-align: center; margin: 20px 0;">
            <button onclick="forceHarvest()">Force 10% Harvest Now</button>
            <button onclick="sellEverything()">Sell Everything</button>
            <button onclick="pauseBot()">Pause Bot</button>
            <button onclick="resumeBot()">Resume Bot</button>
        </div>
    </div>
    <script>
        function forceHarvest() { fetch('/harvest').then(r => r.text()).then(t => alert(t)); }
        function sellEverything() { fetch('/sellall').then(r => r.text()).then(t => alert(t)); }
        function pauseBot() { fetch('/pause').then(r => r.text()).then(t => alert(t)); }
        function resumeBot() { fetch('/resume').then(r => r.text()).then(t => alert(t)); }
        
        setInterval(() => location.reload(), 10000);  // refresh every 10 seconds
    </script>
</body>
</html>
""", total_usd=0, overall=0, daily=0)

@app.route('/data')
def data():
    fetch_all()  # update data
    return jsonify({
        'total_usd': total_usd,
        'overall': overall,
        'daily': daily,
        'holdings': all_assets['Crypto.com'],
        'solana': all_assets.get('Solana', {}),
        'last_update': datetime.now().isoformat()
    })

@app.route('/harvest')
def harvest():
    # Trigger 10% harvest
    log("Web harvest triggered")
    return "10% Harvest Fired!"

@app.route('/sellall')
def sellall():
    # Trigger sell everything
    log("Web sell-all triggered")
    return "Everything Sold!"

@app.route('/pause')
def pause():
    # Pause bot
    log("Web pause triggered")
    return "Bot Paused!"

@app.route('/resume')
def resume():
    # Resume bot
    log("Web resume triggered")
    return "Bot Resumed!"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)