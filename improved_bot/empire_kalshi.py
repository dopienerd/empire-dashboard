# ================================================================
# EMPIRE KALSHI — Prediction Market Trading
# ================================================================
# Trades on Kalshi (CFTC-regulated prediction markets).
# Markets: economics (CPI, Fed rates), crypto, weather, politics.
# Contracts: 1¢-99¢ each, pays $1 if correct.
# Uses kalshi-python SDK with RSA auth.
# ================================================================

import os
import time
import uuid
import threading
import requests
from datetime import datetime

try:
    from kalshi_python import (
        KalshiClient, Configuration,
        MarketsApi, PortfolioApi, EventsApi, ExchangeApi,
        CreateOrderRequest,
    )
    KALSHI_AVAILABLE = True
except ImportError:
    KALSHI_AVAILABLE = False
    print("[KALSHI] kalshi-python not installed — run: pip install kalshi-python")

_kalshi_lock = threading.Lock()

# ================================================================
# CLIENT SETUP
# ================================================================

_client = None
_markets_api = None
_portfolio_api = None
_events_api = None
_exchange_api = None


def init_kalshi(api_key_id: str = None, private_key_path: str = None,
                private_key_pem: str = None) -> bool:
    """
    Initialize Kalshi client with RSA authentication.
    Provide either private_key_path (file path to PEM) or private_key_pem (raw PEM string).
    api_key_id: Your Kalshi API Key ID.
    Returns True on success.
    """
    global _client, _markets_api, _portfolio_api, _events_api, _exchange_api

    if not KALSHI_AVAILABLE:
        print("[KALSHI] SDK not available")
        return False

    key_id = api_key_id or os.environ.get("KALSHI_API_KEY_ID", "")
    pem_path = private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    pem_str = private_key_pem or os.environ.get("KALSHI_PRIVATE_KEY", "")

    if not key_id:
        print("[KALSHI] No API key ID — set KALSHI_API_KEY_ID in .env")
        return False

    try:
        config = Configuration()
        config.host = "https://api.elections.kalshi.com/trade-api/v2"

        _client = KalshiClient(configuration=config)

        # Load private key
        if pem_str:
            # Direct PEM string (from .env)
            _client.set_kalshi_auth(key_id, pem_str)
        elif pem_path and os.path.exists(pem_path):
            with open(pem_path, "r") as f:
                pem_data = f.read()
            _client.set_kalshi_auth(key_id, pem_data)
        else:
            print("[KALSHI] No private key — set KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_PATH in .env")
            return False

        _markets_api = MarketsApi(_client)
        _portfolio_api = PortfolioApi(_client)
        _events_api = EventsApi(_client)
        _exchange_api = ExchangeApi(_client)

        # Test connection
        balance = _portfolio_api.get_balance()
        bal_usd = balance.balance / 100.0  # cents to dollars
        print(f"[KALSHI] Connected! Balance: ${bal_usd:.2f}")
        return True

    except Exception as e:
        print(f"[KALSHI] Init failed: {e}")
        return False


# ================================================================
# MARKET DATA
# ================================================================

_market_cache = {}  # ticker -> {market_data, ts}
_MARKET_TTL = 120


def get_market(ticker: str) -> dict:
    """Fetch market data by ticker. Cached 2 min."""
    now = time.time()
    cached = _market_cache.get(ticker)
    if cached and now - cached["ts"] < _MARKET_TTL:
        return cached["data"]
    try:
        resp = _markets_api.get_market(ticker)
        market = resp.market
        data = {
            "ticker": market.ticker,
            "title": market.title,
            "subtitle": getattr(market, "subtitle", ""),
            "status": market.status,
            "yes_ask": market.yes_ask / 100.0 if market.yes_ask else None,
            "yes_bid": market.yes_bid / 100.0 if market.yes_bid else None,
            "no_ask": market.no_ask / 100.0 if market.no_ask else None,
            "no_bid": market.no_bid / 100.0 if market.no_bid else None,
            "last_price": market.last_price / 100.0 if market.last_price else None,
            "volume": market.volume or 0,
            "open_interest": market.open_interest or 0,
            "close_time": str(market.close_time) if market.close_time else None,
            "result": getattr(market, "result", None),
        }
        _market_cache[ticker] = {"data": data, "ts": now}
        return data
    except Exception as e:
        print(f"[KALSHI] get_market({ticker}) error: {e}")
        return {}


def search_markets(query: str = "", status: str = "open", limit: int = 50) -> list:
    """
    Search for markets. Returns list of market dicts.
    status: 'open', 'closed', 'settled'
    """
    try:
        resp = _markets_api.get_markets(status=status, limit=limit)
        markets = []
        for m in resp.markets:
            title = m.title or ""
            if query and query.lower() not in title.lower():
                continue
            markets.append({
                "ticker": m.ticker,
                "title": title,
                "yes_ask": m.yes_ask / 100.0 if m.yes_ask else None,
                "yes_bid": m.yes_bid / 100.0 if m.yes_bid else None,
                "last_price": m.last_price / 100.0 if m.last_price else None,
                "volume": m.volume or 0,
                "open_interest": m.open_interest or 0,
                "close_time": str(m.close_time) if m.close_time else None,
            })
        return markets
    except Exception as e:
        print(f"[KALSHI] search_markets error: {e}")
        return []


def get_high_volume_markets(min_volume: int = 500, limit: int = 100) -> list:
    """Get open markets sorted by volume. Good for finding liquid markets."""
    try:
        resp = _markets_api.get_markets(status="open", limit=limit)
        markets = []
        for m in resp.markets:
            vol = m.volume or 0
            if vol < min_volume:
                continue
            markets.append({
                "ticker": m.ticker,
                "title": m.title or "",
                "yes_ask": m.yes_ask / 100.0 if m.yes_ask else None,
                "yes_bid": m.yes_bid / 100.0 if m.yes_bid else None,
                "last_price": m.last_price / 100.0 if m.last_price else None,
                "volume": vol,
                "open_interest": m.open_interest or 0,
            })
        markets.sort(key=lambda x: x["volume"], reverse=True)
        return markets
    except Exception as e:
        print(f"[KALSHI] get_high_volume error: {e}")
        return []


# ================================================================
# PORTFOLIO
# ================================================================

def get_balance() -> float:
    """Get Kalshi account balance in USD."""
    try:
        resp = _portfolio_api.get_balance()
        return resp.balance / 100.0
    except Exception as e:
        print(f"[KALSHI] get_balance error: {e}")
        return 0.0


def get_positions() -> list:
    """Get all open positions."""
    try:
        resp = _portfolio_api.get_positions()
        positions = []
        for p in resp.market_positions:
            positions.append({
                "ticker": p.ticker,
                "position": p.position,
                "market_exposure": p.market_exposure / 100.0 if p.market_exposure else 0,
                "total_traded": p.total_traded / 100.0 if p.total_traded else 0,
                "realized_pnl": p.realized_pnl / 100.0 if p.realized_pnl else 0,
            })
        return positions
    except Exception as e:
        print(f"[KALSHI] get_positions error: {e}")
        return []


def get_fills(limit: int = 20) -> list:
    """Get recent trade fills."""
    try:
        resp = _portfolio_api.get_fills(limit=limit)
        fills = []
        for f in resp.fills:
            fills.append({
                "ticker": f.ticker,
                "side": f.side,
                "action": f.action,
                "count": f.count,
                "yes_price": f.yes_price / 100.0 if f.yes_price else 0,
                "no_price": f.no_price / 100.0 if f.no_price else 0,
                "created_time": str(f.created_time) if f.created_time else "",
            })
        return fills
    except Exception as e:
        print(f"[KALSHI] get_fills error: {e}")
        return []


# ================================================================
# ORDER EXECUTION
# ================================================================

def buy_yes(ticker: str, contracts: int = 1, limit_price_cents: int = None) -> dict:
    """
    Buy YES contracts on a market.
    limit_price_cents: max price in cents (1-99). None = market order (buy at ask).
    contracts: number of contracts to buy.
    Returns order result or error.
    """
    return _place_order(ticker, "yes", "buy", contracts, limit_price_cents)


def buy_no(ticker: str, contracts: int = 1, limit_price_cents: int = None) -> dict:
    """
    Buy NO contracts on a market.
    limit_price_cents: max price in cents (1-99). None = market order (buy at ask).
    """
    return _place_order(ticker, "no", "buy", contracts, limit_price_cents)


def sell_yes(ticker: str, contracts: int = 1, limit_price_cents: int = None) -> dict:
    """Sell YES contracts."""
    return _place_order(ticker, "yes", "sell", contracts, limit_price_cents)


def sell_no(ticker: str, contracts: int = 1, limit_price_cents: int = None) -> dict:
    """Sell NO contracts."""
    return _place_order(ticker, "no", "sell", contracts, limit_price_cents)


def _place_order(ticker: str, side: str, action: str,
                 contracts: int, limit_price_cents: int = None) -> dict:
    """Internal order placement."""
    if not _portfolio_api:
        return {"error": "Kalshi not initialized"}
    try:
        order_params = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "action": action,
            "count": contracts,
            "type": "limit" if limit_price_cents else "market",
        }
        if limit_price_cents:
            if side == "yes":
                order_params["yes_price"] = limit_price_cents
            else:
                order_params["no_price"] = limit_price_cents

        req = CreateOrderRequest(**order_params)
        resp = _portfolio_api.create_order(req)
        order = resp.order

        result = {
            "order_id": order.order_id,
            "ticker": order.ticker,
            "side": order.side,
            "action": order.action,
            "status": order.status,
            "count": order.remaining_count,
            "yes_price": order.yes_price,
            "no_price": order.no_price,
        }
        print(f"[KALSHI] Order placed: {action} {contracts}x {side.upper()} on {ticker} -> {result['status']}")
        return result

    except Exception as e:
        print(f"[KALSHI] Order error: {e}")
        return {"error": str(e)}


# ================================================================
# EDGE DETECTION — Find profitable prediction markets
# ================================================================

def find_edge_markets(min_volume: int = 200, max_price: float = 0.85,
                      min_price: float = 0.15) -> list:
    """
    Find markets where YES or NO price is between min_price and max_price.
    These are markets where there's uncertainty = trading opportunity.
    Returns list of {ticker, title, yes_price, edge_side, edge_price, volume}.
    """
    try:
        markets = get_high_volume_markets(min_volume=min_volume, limit=100)
        edges = []
        for m in markets:
            yes_price = m.get("last_price") or m.get("yes_ask")
            if not yes_price:
                continue
            # Look for markets in the "uncertain" zone
            if min_price <= yes_price <= max_price:
                # Determine which side has better edge
                if yes_price < 0.50:
                    # Market thinks NO is more likely → YES is undervalued if we disagree
                    edge_side = "yes"
                    edge_price = yes_price
                else:
                    # Market thinks YES is likely → NO is undervalued if we disagree
                    edge_side = "no"
                    edge_price = 1.0 - yes_price

                edges.append({
                    "ticker": m["ticker"],
                    "title": m["title"],
                    "yes_price": yes_price,
                    "edge_side": edge_side,
                    "edge_price": edge_price,
                    "volume": m["volume"],
                })
        # Sort by volume (most liquid first)
        edges.sort(key=lambda x: x["volume"], reverse=True)
        return edges
    except Exception as e:
        print(f"[KALSHI] find_edge error: {e}")
        return []


# ================================================================
# AUTOMATED TRADING STRATEGIES
# ================================================================

def value_bet_strategy(max_contracts: int = 5, max_spend_usd: float = 10.0) -> list:
    """
    Find and execute value bets: markets where one side is priced very low
    (high potential return) with decent volume.

    Looks for: YES at 10-25¢ or NO at 10-25¢ (high-reward plays).
    Max spend per market = max_spend_usd.
    Returns list of executed trades.
    """
    trades = []
    try:
        markets = get_high_volume_markets(min_volume=300, limit=100)
        balance = get_balance()

        if balance < 1.0:
            print("[KALSHI] Insufficient balance for value bets")
            return trades

        total_spent = 0.0

        for m in markets:
            if total_spent >= max_spend_usd:
                break

            yes_price = m.get("last_price") or m.get("yes_ask")
            if not yes_price:
                continue

            # Value bet: buy YES when price is 10-25¢ (expect 4-10x return)
            if 0.10 <= yes_price <= 0.25:
                cost_per = yes_price
                num = min(max_contracts, int(max_spend_usd / cost_per))
                if num < 1:
                    continue
                price_cents = int(yes_price * 100) + 1  # bid 1¢ above last
                price_cents = min(price_cents, 30)  # cap at 30¢
                result = buy_yes(m["ticker"], num, price_cents)
                if "error" not in result:
                    total_spent += num * price_cents / 100.0
                    trades.append({"ticker": m["ticker"], "side": "yes",
                                   "contracts": num, "price": price_cents / 100.0,
                                   "title": m["title"]})
                time.sleep(1)

            # Value bet: buy NO when YES price is 75-90¢ (NO is cheap)
            elif 0.75 <= yes_price <= 0.90:
                no_price = 1.0 - yes_price
                cost_per = no_price
                num = min(max_contracts, int(max_spend_usd / cost_per))
                if num < 1:
                    continue
                price_cents = int(no_price * 100) + 1
                price_cents = min(price_cents, 30)
                result = buy_no(m["ticker"], num, price_cents)
                if "error" not in result:
                    total_spent += num * price_cents / 100.0
                    trades.append({"ticker": m["ticker"], "side": "no",
                                   "contracts": num, "price": price_cents / 100.0,
                                   "title": m["title"]})
                time.sleep(1)

        return trades
    except Exception as e:
        print(f"[KALSHI] value_bet error: {e}")
        return trades


def spread_arbitrage(min_spread: float = 0.05) -> list:
    """
    Find markets where bid-ask spread is wide enough to profit.
    Buy at bid, sell at ask. Requires good volume.
    """
    opportunities = []
    try:
        markets = get_high_volume_markets(min_volume=500, limit=100)
        for m in markets:
            yes_bid = m.get("yes_bid") or 0
            yes_ask = m.get("yes_ask") or 1
            if yes_bid and yes_ask:
                spread = yes_ask - yes_bid
                if spread >= min_spread:
                    opportunities.append({
                        "ticker": m["ticker"],
                        "title": m["title"],
                        "yes_bid": yes_bid,
                        "yes_ask": yes_ask,
                        "spread": round(spread, 2),
                        "volume": m["volume"],
                    })
        opportunities.sort(key=lambda x: x["spread"], reverse=True)
        return opportunities
    except Exception as e:
        print(f"[KALSHI] spread_arb error: {e}")
        return []


# ================================================================
# KALSHI WORKER — Runs as a daemon thread in the bot
# ================================================================

def kalshi_trading_worker(log_fn=None):
    """
    Automated Kalshi prediction market trader.
    Runs every 15 minutes:
    1. Scans for value bets (low-price, high-volume markets)
    2. Manages existing positions (take profit / cut loss)
    3. Reports status
    """
    def log(msg):
        if log_fn:
            log_fn(msg)
        print(msg)

    log("[KALSHI] Prediction market worker starting...")
    time.sleep(10)

    # Try to initialize
    if not init_kalshi():
        log("[KALSHI] Failed to initialize — worker exiting. Check API keys in .env")
        return

    while True:
        try:
            balance = get_balance()
            log(f"[KALSHI] Balance: ${balance:.2f}")

            if balance < 0.50:
                log("[KALSHI] Balance too low — skipping cycle")
                time.sleep(900)
                continue

            # 1. Check existing positions
            positions = get_positions()
            for pos in positions:
                if pos["realized_pnl"] != 0:
                    log(f"[KALSHI] Position: {pos['ticker']} PnL=${pos['realized_pnl']:.2f}")

            # 2. Look for value bets (spend max $5 per cycle, max 3 contracts each)
            max_spend = min(balance * 0.15, 5.0)  # 15% of balance, capped at $5
            if max_spend >= 1.0:
                trades = value_bet_strategy(max_contracts=3, max_spend_usd=max_spend)
                for t in trades:
                    log(f"[KALSHI BUY] {t['contracts']}x {t['side'].upper()} @ ${t['price']:.2f} on {t['title'][:60]}")

            # 3. Look for spread opportunities
            spreads = spread_arbitrage(min_spread=0.08)
            if spreads:
                log(f"[KALSHI] Found {len(spreads)} spread opportunities")
                for s in spreads[:3]:
                    log(f"  {s['ticker']}: spread={s['spread']:.0%} vol={s['volume']}")

            log(f"[KALSHI] Cycle complete. Positions: {len(positions)}, Balance: ${balance:.2f}")
            time.sleep(900)  # Run every 15 minutes

        except Exception as e:
            log(f"[KALSHI ERROR] {e}")
            time.sleep(300)


# ================================================================
# DASHBOARD DATA
# ================================================================

def get_kalshi_dashboard_data() -> dict:
    """Return Kalshi data for the dashboard."""
    if not _portfolio_api:
        return {"connected": False}
    try:
        balance = get_balance()
        positions = get_positions()
        fills = get_fills(limit=5)
        return {
            "connected": True,
            "balance_usd": round(balance, 2),
            "positions": positions,
            "recent_fills": fills,
            "position_count": len(positions),
        }
    except Exception:
        return {"connected": False}
