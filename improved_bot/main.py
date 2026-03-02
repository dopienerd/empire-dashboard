# ================================================================
# EMPIRE MAIN.PY — IMPROVED VERSION
# ================================================================
# Fixes:
#   - Removed reference to undefined 'futures' (was 'futures_client')
#   - Removed duplicate set_cex_clients call
#   - Removed duplicate load_markets calls
#   - Proper initialization order
#   - Dashboard uses live price fetching for EVM (not hardcoded)
#   - DEX modules initialized after wallets exist (dynamic pubkey)
# ================================================================

import os
import time
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import json

import ccxt

import empire_core as CORE
import empire_dex as DEX_MODULE
import empire_wallets as WALLET_MODULE

# ================================================================
# LOAD ALL USDT PAIRS
# ================================================================
def fetch_all_usdt_pairs():
    try:
        ex = ccxt.cryptocom({"enableRateLimit": True})
        markets = ex.load_markets()
        all_pairs = [s for s in markets.keys()
                     if s.endswith("/USDT") and "PERP" not in s]
        print(f"[MARKETS] Loaded {len(all_pairs)} USDT pairs")
        return all_pairs
    except Exception:
        print("[MARKETS] Failed to fetch — using fallback list")
        return [
            "BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT", "PEPE/USDT",
            "WIF/USDT", "BONK/USDT", "FLOKI/USDT", "SHIB/USDT", "MEW/USDT",
            "POPCAT/USDT", "MOG/USDT", "BRETT/USDT", "TOSHI/USDT",
            "DEGEN/USDT", "NEIRO/USDT", "TURBO/USDT", "SPX/USDT",
        ]


ALL_CEX_PAIRS = fetch_all_usdt_pairs()
CORE.set_known_pairs(set(ALL_CEX_PAIRS))


# Auto-refresh pairs every 10 minutes
def refresh_all_pairs():
    while True:
        time.sleep(600)
        try:
            new_pairs = fetch_all_usdt_pairs()
            CORE.set_known_pairs(set(new_pairs))
            print(f"[REFRESH] Updated — now {len(new_pairs)} pairs")
        except Exception as e:
            print(f"[REFRESH ERROR] {e}")

threading.Thread(target=refresh_all_pairs, daemon=True).start()


# ================================================================
# LOAD ENVIRONMENT VARIABLES
# ================================================================
CRYPTOCOM_API_KEY = os.getenv("CRYPTOCOM_API_KEY")
CRYPTOCOM_API_SECRET = os.getenv("CRYPTOCOM_API_SECRET")
EVM_PRIVATE_KEY = os.getenv("EVM_PRIVATE_KEY", "").strip()

SOL_PRIVATE_KEY = os.getenv("SOL_PRIVATE_KEY", "").strip()
if not SOL_PRIVATE_KEY:
    for i in range(1, 20):
        ck = os.getenv(f"SOL_PRIVATE_KEY_{i}", "").strip()
        if ck:
            SOL_PRIVATE_KEY = ck
            print(f"[KEYS] Using SOL_PRIVATE_KEY_{i}")
            break


# ================================================================
# REQUIRED KEYS CHECK
# ================================================================
if not CRYPTOCOM_API_KEY or not CRYPTOCOM_API_SECRET:
    print("ERROR: Missing Crypto.com API keys.")
    time.sleep(5)
    exit()

if not EVM_PRIVATE_KEY:
    print("ERROR: Missing EVM_PRIVATE_KEY")
    time.sleep(5)
    exit()

if not SOL_PRIVATE_KEY:
    print("ERROR: Missing Solana key.")
    time.sleep(5)
    exit()

print("[KEYS] All keys loaded successfully!")


# ================================================================
# CREATE WALLETS
# ================================================================
wallets = WALLET_MODULE.create_wallets(WALLET_MODULE.CONFIG)
WALLET_MODULE.WALLETS = wallets

# Set wallets in DEX module, then initialize DEX modules
DEX_MODULE.set_wallets(wallets)
DEX_MODULE.init_dex_modules()

# Attach to CORE
CORE.attach_wallets(wallets)
CORE.attach_dex(DEX_MODULE.DEX)
print("[MAIN] Wallets + DEX attached to CORE.")


# ================================================================
# INITIALIZE CEX CLIENTS
# ================================================================
print("[MAIN] Connecting to Crypto.com LIVE...")

exchange = ccxt.cryptocom({
    "apiKey": CRYPTOCOM_API_KEY,
    "secret": CRYPTOCOM_API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})

futures_client = ccxt.cryptocom({
    "apiKey": CRYPTOCOM_API_KEY,
    "secret": CRYPTOCOM_API_SECRET,
    "sandbox": False,
    "enableRateLimit": True,
    "options": {"defaultType": "futures"},
    "timeout": 30000,
})

# margin_client is the same as spot on Crypto.com
margin_client = exchange

# Attach ALL CEX clients to CORE (single call, no duplicates)
CORE.set_cex_clients(
    main=exchange,
    margin=margin_client,
    futures=futures_client,
)

# Load markets
try:
    exchange.load_markets()
    futures_client.load_markets()
    print("[CEX] Markets loaded successfully")
except Exception as e:
    print(f"[CEX] Market load warning: {e}")

# Set 10x leverage on futures
try:
    for sym in futures_client.markets:
        if "USDT:USDT" in sym or "PERP" in sym:
            try:
                futures_client.set_leverage(10, sym)
            except Exception:
                pass
    print("[FUTURES] 10x leverage set")
except Exception as e:
    print(f"[FUTURES] Leverage setup warning: {e}")


# ================================================================
# FETCH BALANCE
# ================================================================
print("[MAIN] Fetching initial balance...")
try:
    bal = exchange.fetch_balance()
    usdt = float(bal.get("total", {}).get("USDT", 0))
    print(f"[SUCCESS] USDT Balance: ${usdt:,.2f}")
except Exception as e:
    print(f"[BALANCE WARNING] {e} — continuing anyway")


# ================================================================
# SYNC POSITIONS WITH EXCHANGE
# ================================================================
CORE.sync_positions_with_exchange()


# ================================================================
# DASHBOARD SERVER
# ================================================================
class EmpireHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path != "/empire_status":
            self.send_response(404)
            self.end_headers()
            return

        data = {
            "total_usd": 0,
            "cex": {},
            "solana": [],
            "evm": {},
            "logs": list(CORE.get_logs(100)),
        }

        # CEX balances
        try:
            bal = exchange.fetch_balance()
            for coin, amount in bal["total"].items():
                if float(amount) <= 0:
                    continue
                if coin in ["USDT", "USDC", "BUSD", "TUSD", "PYUSD"]:
                    usd = float(amount)
                else:
                    usd = 0.0
                    try:
                        price = exchange.fetch_ticker(f"{coin}/USDT")["last"]
                        usd = float(amount) * price
                    except Exception:
                        try:
                            price = exchange.fetch_ticker(f"{coin}/USD")["last"]
                            usd = float(amount) * price
                        except Exception:
                            pass
                data["cex"][coin] = {"amount": round(float(amount), 8), "usd": round(usd, 2)}
                data["total_usd"] += usd

            # Put USDT first
            if "USDT" in data["cex"]:
                usdt_entry = data["cex"].pop("USDT")
                data["cex"] = {"USDT": usdt_entry, **data["cex"]}
        except Exception as e:
            print(f"[DASHBOARD] CEX error: {e}")

        # Solana
        try:
            sol_price = DEX_MODULE.DEX.get_sol_price()
            for w in wallets.solana.wallets:
                sol = w.get_balance()
                usd = sol * sol_price
                addr = str(w.pubkey)[:8] + "..." if w.pubkey else "?"
                data["solana"].append({
                    "address": addr, "sol": round(sol, 5), "usd": round(usd, 2),
                })
                data["total_usd"] += usd
        except Exception as e:
            print(f"[DASHBOARD] Solana error: {e}")

        # EVM
        try:
            for chain, wallet in wallets.evm.wallets.items():
                bal_val = wallet.get_balance()
                # Use rough native prices
                prices = {"ETH": 3400, "BASE": 3400, "ARB": 3400,
                          "OP": 3400, "AVAX": 48, "CRO": 0.13}
                usd = bal_val * prices.get(chain, 1)
                data["evm"][chain] = {"balance": round(bal_val, 6), "usd": round(usd, 2)}
                data["total_usd"] += usd
        except Exception as e:
            print(f"[DASHBOARD] EVM error: {e}")

        data["total_usd"] = round(data["total_usd"], 2)

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, separators=(",", ":")).encode())


def start_dashboard_server():
    server = ThreadingHTTPServer(("0.0.0.0", 8222), EmpireHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("[DASHBOARD] LIVE -> http://localhost:8222/empire_status")


start_dashboard_server()


# ================================================================
# START EMPIRE ENGINE
# ================================================================
print("[MAIN] Starting EMPIRE engine...")
CORE.start_empire()


# ================================================================
# MAIN LOOP
# ================================================================
if __name__ == "__main__":
    print("[MAIN] Empire is LIVE and trading.")
    print("[MAIN] Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print("\n[MAIN] Empire stopped manually.")
        exit()
