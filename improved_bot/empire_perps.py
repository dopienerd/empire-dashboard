# empire_perps.py — IMPROVED VERSION
# Fixes:
#   - Added crash resilience (each worker catches all exceptions)
#   - Cached CoinGecko prices (avoids rate limit 429 errors)
#   - Higher risk tolerance: 10x leverage, 25% position size, more positions
#   - Stop loss / take profit adjusted for high risk

import os
import time
import json
import requests
from typing import Optional, Dict, Any

# ============================================================
# CONFIGURATION — HIGH RISK TOLERANCE
# ============================================================
PERPS_CONFIG = {
    "MAX_LEVERAGE": 10,
    "POSITION_SIZE_PCT": 0.25,
    "STOP_LOSS_PCT": 8,
    "TAKE_PROFIT_PCT": 15,
    "MIN_TRADE_USD": 5,
    "MAX_POSITIONS": 5,
    "SCAN_INTERVAL": 20,
    "MOMENTUM_THRESHOLD": 1.5,
}

perp_positions = {}

# ============================================================
# SHARED PRICE CACHE (avoids CoinGecko rate limits)
# ============================================================
_price_cache = {}  # {"bitcoin": {"price": 60000, "change_24h": 2.5, "ts": time.time()}}
_CACHE_TTL = 60  # seconds


def _fetch_cg_price(cg_id: str) -> Optional[dict]:
    """Fetch price from CoinGecko with 60s cache."""
    now = time.time()
    cached = _price_cache.get(cg_id)
    if cached and now - cached["ts"] < _CACHE_TTL:
        return cached

    try:
        r = requests.get(
            f"https://api.coingecko.com/api/v3/simple/price"
            f"?ids={cg_id}&vs_currencies=usd&include_24hr_change=true",
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json().get(cg_id, {})
            result = {
                "price": data.get("usd", 0),
                "change_24h": data.get("usd_24h_change", 0),
                "ts": now,
            }
            _price_cache[cg_id] = result
            return result
    except Exception:
        pass

    return cached  # return stale cache if fetch fails


CG_MAP = {
    "SOL-PERP": "solana", "BTC-PERP": "bitcoin", "ETH-PERP": "ethereum",
    "ETH": "ethereum", "BTC": "bitcoin", "LINK": "chainlink",
    "SOL": "solana",
}


# ============================================================
# JUPITER PERPETUALS (SOLANA)
# ============================================================
class JupiterPerps:
    def __init__(self, wallet):
        self.wallet = wallet
        self.positions = {}
        print("[JUPITER PERPS] Initialized")

    def get_sol_balance(self) -> float:
        try:
            if self.wallet and hasattr(self.wallet, "solana"):
                return self.wallet.solana.get_total_balance()
        except Exception:
            pass
        return 0

    def get_market_price(self, market: str) -> Optional[float]:
        cg_id = CG_MAP.get(market)
        if not cg_id:
            return None
        data = _fetch_cg_price(cg_id)
        return data["price"] if data else None

    def get_change_24h(self, market: str) -> float:
        cg_id = CG_MAP.get(market)
        if not cg_id:
            return 0
        data = _fetch_cg_price(cg_id)
        return data["change_24h"] if data else 0

    def open_position(self, market: str, side: str, size_usd: float,
                      leverage: int = 10) -> Optional[str]:
        try:
            leverage = min(leverage, PERPS_CONFIG["MAX_LEVERAGE"])
            price = self.get_market_price(market)
            if not price:
                return None

            collateral_usd = size_usd / leverage
            sol_price = self.get_market_price("SOL-PERP") or 230
            collateral_sol = collateral_usd / sol_price

            if self.get_sol_balance() < collateral_sol + 0.01:
                return None

            position_id = f"jupiter_{market}_{int(time.time())}"
            self.positions[market] = {
                "id": position_id, "side": side, "entry_price": price,
                "size_usd": size_usd, "leverage": leverage,
                "collateral_sol": collateral_sol, "ts": time.time(),
                "chain": "solana",
            }
            perp_positions[market] = self.positions[market]

            print(f"[JUPITER] {side.upper()} {market} @ ${price:.2f} | ${size_usd:.2f} | {leverage}x")
            return position_id
        except Exception as e:
            print(f"[JUPITER] Open error: {e}")
            return None

    def close_position(self, market: str) -> bool:
        try:
            if market not in self.positions:
                return False
            pos = self.positions[market]
            price = self.get_market_price(market)
            if not price:
                return False

            entry = pos["entry_price"]
            if pos["side"] == "long":
                pnl_pct = (price - entry) / entry * 100 * pos["leverage"]
            else:
                pnl_pct = (entry - price) / entry * 100 * pos["leverage"]

            pnl_usd = pos["size_usd"] * (pnl_pct / 100)
            del self.positions[market]
            perp_positions.pop(market, None)

            result = f"+${pnl_usd:.2f}" if pnl_usd > 0 else f"-${abs(pnl_usd):.2f}"
            print(f"[JUPITER] Closed {market} | PnL: {result} ({pnl_pct:+.1f}%)")
            return True
        except Exception as e:
            print(f"[JUPITER] Close error: {e}")
            return False

    def check_positions(self):
        for market, pos in list(self.positions.items()):
            try:
                price = self.get_market_price(market)
                if not price:
                    continue
                entry = pos["entry_price"]
                lev = pos["leverage"]
                if pos["side"] == "long":
                    pnl_pct = (price - entry) / entry * 100 * lev
                else:
                    pnl_pct = (entry - price) / entry * 100 * lev

                if pnl_pct <= -PERPS_CONFIG["STOP_LOSS_PCT"]:
                    print(f"[JUPITER] SL triggered {market}")
                    self.close_position(market)
                elif pnl_pct >= PERPS_CONFIG["TAKE_PROFIT_PCT"]:
                    print(f"[JUPITER] TP triggered {market}")
                    self.close_position(market)
            except Exception:
                continue


# ============================================================
# GMX PERPETUALS (ARBITRUM)
# ============================================================
class GMXPerps:
    def __init__(self, wallet):
        self.wallet = wallet
        self.positions = {}
        print("[GMX PERPS] Initialized")

    def get_eth_balance(self) -> float:
        try:
            if self.wallet and hasattr(self.wallet, "evm"):
                arb = self.wallet.evm.wallets.get("ARB")
                if arb:
                    return arb.get_balance()
        except Exception:
            pass
        return 0

    def get_market_price(self, symbol: str) -> Optional[float]:
        cg_id = CG_MAP.get(symbol)
        if not cg_id:
            return None
        data = _fetch_cg_price(cg_id)
        return data["price"] if data else None

    def get_change_24h(self, symbol: str) -> float:
        cg_id = CG_MAP.get(symbol)
        if not cg_id:
            return 0
        data = _fetch_cg_price(cg_id)
        return data["change_24h"] if data else 0

    def open_position(self, symbol: str, side: str, size_usd: float,
                      leverage: int = 10) -> Optional[str]:
        try:
            leverage = min(leverage, PERPS_CONFIG["MAX_LEVERAGE"])
            price = self.get_market_price(symbol)
            if not price:
                return None

            collateral_usd = size_usd / leverage
            eth_price = self.get_market_price("ETH") or 3400
            collateral_eth = collateral_usd / eth_price

            if self.get_eth_balance() < collateral_eth + 0.001:
                return None

            market = f"{symbol}-USD"
            position_id = f"gmx_{symbol}_{int(time.time())}"
            self.positions[market] = {
                "id": position_id, "side": side, "entry_price": price,
                "size_usd": size_usd, "leverage": leverage, "ts": time.time(),
                "chain": "arbitrum",
            }
            perp_positions[market] = self.positions[market]

            print(f"[GMX] {side.upper()} {market} @ ${price:.2f} | ${size_usd:.2f} | {leverage}x")
            return position_id
        except Exception as e:
            print(f"[GMX] Open error: {e}")
            return None

    def close_position(self, market: str) -> bool:
        try:
            if market not in self.positions:
                return False
            pos = self.positions[market]
            symbol = market.split("-")[0]
            price = self.get_market_price(symbol)
            if not price:
                return False

            entry = pos["entry_price"]
            lev = pos["leverage"]
            if pos["side"] == "long":
                pnl_pct = (price - entry) / entry * 100 * lev
            else:
                pnl_pct = (entry - price) / entry * 100 * lev

            pnl_usd = pos["size_usd"] * (pnl_pct / 100)
            del self.positions[market]
            perp_positions.pop(market, None)

            result = f"+${pnl_usd:.2f}" if pnl_usd > 0 else f"-${abs(pnl_usd):.2f}"
            print(f"[GMX] Closed {market} | PnL: {result} ({pnl_pct:+.1f}%)")
            return True
        except Exception:
            return False

    def check_positions(self):
        for market, pos in list(self.positions.items()):
            try:
                symbol = market.split("-")[0]
                price = self.get_market_price(symbol)
                if not price:
                    continue
                entry = pos["entry_price"]
                lev = pos["leverage"]
                if pos["side"] == "long":
                    pnl_pct = (price - entry) / entry * 100 * lev
                else:
                    pnl_pct = (entry - price) / entry * 100 * lev

                if pnl_pct <= -PERPS_CONFIG["STOP_LOSS_PCT"]:
                    self.close_position(market)
                elif pnl_pct >= PERPS_CONFIG["TAKE_PROFIT_PCT"]:
                    self.close_position(market)
            except Exception:
                continue


# ============================================================
# BASE PERPETUALS (Synthetix/Kwenta)
# ============================================================
class BasePerps:
    def __init__(self, wallet):
        self.wallet = wallet
        self.positions = {}
        print("[BASE PERPS] Initialized")

    def get_eth_balance(self) -> float:
        try:
            if self.wallet and hasattr(self.wallet, "evm"):
                base = self.wallet.evm.wallets.get("BASE")
                if base:
                    return base.get_balance()
        except Exception:
            pass
        return 0

    def get_market_price(self, symbol: str) -> Optional[float]:
        cg_id = CG_MAP.get(symbol)
        if not cg_id:
            return None
        data = _fetch_cg_price(cg_id)
        return data["price"] if data else None

    def get_change_24h(self, symbol: str) -> float:
        cg_id = CG_MAP.get(symbol)
        if not cg_id:
            return 0
        data = _fetch_cg_price(cg_id)
        return data["change_24h"] if data else 0

    def open_position(self, symbol: str, side: str, size_usd: float,
                      leverage: int = 10) -> Optional[str]:
        try:
            leverage = min(leverage, PERPS_CONFIG["MAX_LEVERAGE"])
            price = self.get_market_price(symbol)
            if not price:
                return None

            collateral_usd = size_usd / leverage
            eth_price = self.get_market_price("ETH") or 3400
            collateral_eth = collateral_usd / eth_price

            if self.get_eth_balance() < collateral_eth + 0.0005:
                return None

            market = f"{symbol}-PERP-BASE"
            position_id = f"base_{symbol}_{int(time.time())}"
            self.positions[market] = {
                "id": position_id, "side": side, "entry_price": price,
                "size_usd": size_usd, "leverage": leverage, "ts": time.time(),
                "chain": "base",
            }
            perp_positions[market] = self.positions[market]

            print(f"[BASE] {side.upper()} {market} @ ${price:.2f} | ${size_usd:.2f} | {leverage}x")
            return position_id
        except Exception as e:
            print(f"[BASE] Open error: {e}")
            return None

    def close_position(self, market: str) -> bool:
        try:
            if market not in self.positions:
                return False
            pos = self.positions[market]
            symbol = market.split("-")[0]
            price = self.get_market_price(symbol)
            if not price:
                return False

            entry = pos["entry_price"]
            lev = pos["leverage"]
            if pos["side"] == "long":
                pnl_pct = (price - entry) / entry * 100 * lev
            else:
                pnl_pct = (entry - price) / entry * 100 * lev

            pnl_usd = pos["size_usd"] * (pnl_pct / 100)
            del self.positions[market]
            perp_positions.pop(market, None)

            result = f"+${pnl_usd:.2f}" if pnl_usd > 0 else f"-${abs(pnl_usd):.2f}"
            print(f"[BASE] Closed {market} | PnL: {result} ({pnl_pct:+.1f}%)")
            return True
        except Exception:
            return False

    def check_positions(self):
        for market, pos in list(self.positions.items()):
            try:
                symbol = market.split("-")[0]
                price = self.get_market_price(symbol)
                if not price:
                    continue
                entry = pos["entry_price"]
                lev = pos["leverage"]
                if pos["side"] == "long":
                    pnl_pct = (price - entry) / entry * 100 * lev
                else:
                    pnl_pct = (entry - price) / entry * 100 * lev

                if pnl_pct <= -PERPS_CONFIG["STOP_LOSS_PCT"]:
                    self.close_position(market)
                elif pnl_pct >= PERPS_CONFIG["TAKE_PROFIT_PCT"]:
                    self.close_position(market)
            except Exception:
                continue


# ============================================================
# UNIFIED PERPS MANAGER
# ============================================================
class PerpetualsManager:
    def __init__(self, wallets):
        self.wallets = wallets
        self.jupiter = JupiterPerps(wallets)
        self.gmx = GMXPerps(wallets)
        self.base = BasePerps(wallets)
        print("[PERPS MANAGER] All chains initialized")

    def check_all_positions(self):
        self.jupiter.check_positions()
        self.gmx.check_positions()
        self.base.check_positions()

    def get_total_exposure(self) -> float:
        return sum(p.get("size_usd", 0) for p in perp_positions.values())


# ============================================================
# PERPS TRADING WORKER
# ============================================================
def perps_trading_worker():
    print("[PERPS WORKER] Starting perpetuals trader...")
    time.sleep(10)

    try:
        from empire_wallets import WALLETS
        if not WALLETS:
            print("[PERPS WORKER] No wallets — exiting")
            return
        perps = PerpetualsManager(WALLETS)
    except Exception as e:
        print(f"[PERPS WORKER] Init failed: {e}")
        return

    print("[PERPS WORKER] Ready!")

    markets = {
        "jupiter": ["SOL-PERP", "BTC-PERP", "ETH-PERP"],
        "gmx": ["ETH", "BTC"],
        "base": ["ETH", "BTC"],
    }

    while True:
        try:
            perps.check_all_positions()

            if len(perp_positions) >= PERPS_CONFIG["MAX_POSITIONS"]:
                time.sleep(PERPS_CONFIG["SCAN_INTERVAL"])
                continue

            # --- JUPITER ---
            sol_bal = perps.jupiter.get_sol_balance()
            if sol_bal >= 0.03:
                for market in markets["jupiter"]:
                    if market in perp_positions:
                        continue
                    try:
                        change = perps.jupiter.get_change_24h(market)
                        sol_price = perps.jupiter.get_market_price("SOL-PERP") or 230
                        size = sol_bal * sol_price * PERPS_CONFIG["POSITION_SIZE_PCT"] * PERPS_CONFIG["MAX_LEVERAGE"]

                        if size >= PERPS_CONFIG["MIN_TRADE_USD"]:
                            if change >= PERPS_CONFIG["MOMENTUM_THRESHOLD"]:
                                perps.jupiter.open_position(market, "long", size)
                                break
                            elif change <= -PERPS_CONFIG["MOMENTUM_THRESHOLD"]:
                                perps.jupiter.open_position(market, "short", size)
                                break
                    except Exception:
                        continue

            # --- GMX ---
            arb_bal = perps.gmx.get_eth_balance()
            if arb_bal >= 0.003:
                for symbol in markets["gmx"]:
                    market = f"{symbol}-USD"
                    if market in perp_positions:
                        continue
                    try:
                        change = perps.gmx.get_change_24h(symbol)
                        eth_price = perps.gmx.get_market_price("ETH") or 3400
                        size = arb_bal * eth_price * PERPS_CONFIG["POSITION_SIZE_PCT"] * PERPS_CONFIG["MAX_LEVERAGE"]

                        if size >= PERPS_CONFIG["MIN_TRADE_USD"]:
                            if change >= PERPS_CONFIG["MOMENTUM_THRESHOLD"]:
                                perps.gmx.open_position(symbol, "long", size)
                                break
                            elif change <= -PERPS_CONFIG["MOMENTUM_THRESHOLD"]:
                                perps.gmx.open_position(symbol, "short", size)
                                break
                    except Exception:
                        continue

            # --- BASE ---
            base_bal = perps.base.get_eth_balance()
            if base_bal >= 0.002:
                for symbol in markets["base"]:
                    market = f"{symbol}-PERP-BASE"
                    if market in perp_positions:
                        continue
                    try:
                        change = perps.base.get_change_24h(symbol)
                        eth_price = perps.base.get_market_price("ETH") or 3400
                        size = base_bal * eth_price * PERPS_CONFIG["POSITION_SIZE_PCT"] * PERPS_CONFIG["MAX_LEVERAGE"]

                        if size >= PERPS_CONFIG["MIN_TRADE_USD"]:
                            if change >= PERPS_CONFIG["MOMENTUM_THRESHOLD"]:
                                perps.base.open_position(symbol, "long", size)
                                break
                            elif change <= -PERPS_CONFIG["MOMENTUM_THRESHOLD"]:
                                perps.base.open_position(symbol, "short", size)
                                break
                    except Exception:
                        continue

            time.sleep(PERPS_CONFIG["SCAN_INTERVAL"])

        except Exception as e:
            print(f"[PERPS WORKER ERROR] {e}")
            time.sleep(60)


# ============================================================
# PERPS MONITOR
# ============================================================
def perps_monitor():
    print("[PERPS MONITOR] Starting...")
    time.sleep(15)

    while True:
        try:
            if not perp_positions:
                time.sleep(30)
                continue

            for market, pos in perp_positions.items():
                age_min = (time.time() - pos["ts"]) / 60
                print(f"[PERPS] {market}: {pos['side'].upper()} @ ${pos['entry_price']:.2f} | "
                      f"{pos['leverage']}x | Age: {age_min:.0f}min")

            time.sleep(300)
        except Exception:
            time.sleep(60)
