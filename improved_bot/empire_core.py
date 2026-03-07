"""
EMPIRE CORE — IMPROVED VERSION (Part 1: Infrastructure + Helpers)
=================================================================
Crypto trading bot core: config, state, thread safety, position
management, CEX order helpers, balance tracking, DB logging,
trend filter, trailing exit, stop-loss, and auto-restart logic.

Key fixes vs. original:
  - Zero duplicate function definitions
  - get_trend_filter() defined (was missing, crashed ~8 workers)
  - stop_loss_worker() defined (was in workers list but missing)
  - STALE_CONFIG dict defined (was referenced but missing)
  - recently_sold dict defined (was referenced but missing)
  - TRADE_SIZE_FRACTION defined (was referenced but missing)
  - hot_chain initialized (was referenced but missing)
  - Thread-safe position dict access via locks
  - Simplified spot_sell (old version blocked stop-loss sells)
  - High risk tolerance tuning applied
"""

# =========================================================================
# 1. IMPORTS
# =========================================================================
import os
import sys
import time
import json
import math
import random
import sqlite3
import hashlib
import threading
import traceback
from datetime import datetime, timedelta
from collections import defaultdict, deque

# ccxt for CEX (Binance, Bybit, etc.)
try:
    import ccxt
except ImportError:
    ccxt = None
    print("[WARN] ccxt not installed — CEX trading disabled")

# requests for HTTP calls
import requests

# AI Signal Engine
try:
    import empire_ai as AI
except ImportError:
    AI = None
    print("[WARN] empire_ai not found — running without AI signals")

# =========================================================================
# 2. THREAD SAFETY — LOCKS
# =========================================================================
positions_lock = threading.Lock()
futures_lock = threading.Lock()
scalp_lock = threading.Lock()
log_lock = threading.Lock()
db_lock = threading.Lock()
stats_lock = threading.Lock()
listing_lock = threading.Lock()
capital_lock = threading.Lock()

# =========================================================================
# 3. LOG BUFFER
# =========================================================================
_log_buffer = deque(maxlen=500)


def log(msg: str):
    """Thread-safe log append with timestamp."""
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    with log_lock:
        _log_buffer.append(entry)


def get_logs(n: int = 50) -> list:
    """Return last n log entries."""
    with log_lock:
        return list(_log_buffer)[-n:]


# =========================================================================
# 4. PERSISTENT NEW-LISTING MEMORY
# =========================================================================
LISTING_MEMORY_FILE = "listing_memory.json"
_listing_memory = {}


def _load_listing_memory():
    global _listing_memory
    try:
        if os.path.exists(LISTING_MEMORY_FILE):
            with open(LISTING_MEMORY_FILE, "r") as f:
                _listing_memory = json.load(f)
    except Exception:
        _listing_memory = {}


def _save_listing_memory():
    try:
        with open(LISTING_MEMORY_FILE, "w") as f:
            json.dump(_listing_memory, f)
    except Exception:
        pass


def remember_listing(symbol: str, data: dict):
    """Store a new listing in persistent memory."""
    with listing_lock:
        _listing_memory[symbol] = {
            **data,
            "seen_at": time.time(),
        }
        _save_listing_memory()


def is_listing_known(symbol: str) -> bool:
    with listing_lock:
        return symbol in _listing_memory


def get_listing_memory() -> dict:
    with listing_lock:
        return dict(_listing_memory)


_load_listing_memory()

# =========================================================================
# 5. DASHBOARD SUPPORT
# =========================================================================
_dashboard_state = {
    "started_at": datetime.now().isoformat(),
    "total_trades": 0,
    "total_pnl_usd": 0.0,
    "total_fees_usd": 0.0,
    "wins": 0,
    "losses": 0,
    "best_trade": None,
    "worst_trade": None,
    "last_trade": None,
}


def dashboard_snapshot() -> dict:
    """Return a snapshot of the bot state for the dashboard."""
    with positions_lock:
        pos_copy = dict(positions)
    with futures_lock:
        fut_copy = dict(futures_positions)
    with scalp_lock:
        scalp_copy = dict(scalp_positions)
    with stats_lock:
        stats_copy = dict(_dashboard_state)
    result = {
        **stats_copy,
        "positions": pos_copy,
        "futures_positions": fut_copy,
        "scalp_positions": scalp_copy,
        "logs": get_logs(30),
        "trend": get_trend_filter(),
        "hot_chain": hot_chain,
    }

    # Add AI data if available
    if AI and exchange:
        try:
            symbols = list(pos_copy.keys())[:15]
            result["ai_data"] = AI.get_dashboard_ai_data(exchange, symbols)
        except Exception:
            result["ai_data"] = None

    return result


def record_trade_result(symbol, pnl_usd, pnl_pct, side="buy", fee_usd=0.0):
    """Update dashboard stats after a trade."""
    with stats_lock:
        _dashboard_state["total_trades"] += 1
        _dashboard_state["total_pnl_usd"] += pnl_usd
        _dashboard_state["total_fees_usd"] += fee_usd
        if pnl_usd >= 0:
            _dashboard_state["wins"] += 1
        else:
            _dashboard_state["losses"] += 1
        trade_info = {
            "symbol": symbol,
            "pnl_usd": round(pnl_usd, 2),
            "pnl_pct": round(pnl_pct, 2),
            "time": datetime.now().isoformat(),
        }
        _dashboard_state["last_trade"] = trade_info
        if (
            _dashboard_state["best_trade"] is None
            or pnl_usd > _dashboard_state["best_trade"]["pnl_usd"]
        ):
            _dashboard_state["best_trade"] = trade_info
        if (
            _dashboard_state["worst_trade"] is None
            or pnl_usd < _dashboard_state["worst_trade"]["pnl_usd"]
        ):
            _dashboard_state["worst_trade"] = trade_info


# =========================================================================
# 6. GLOBAL CONFIG / STATE VARIABLES
# =========================================================================

# --- HIGH RISK / AGGRESSIVE SETTINGS ---
MAX_CONCURRENT_POSITIONS = 15
MAX_MARGIN_POSITIONS = 10
COOLDOWN_SECONDS = 60
MIN_TRADE_SIZE_USD = 5
MAX_TRADE_SIZE_USD = 5000
MIN_24H_VOLUME = 50_000
WORKER_CAPITAL_FRACTION = 0.60
TRADE_SIZE_FRACTION = 0.70

# --- EXCHANGE FEE CONSTANTS ---
EXCHANGE_FEE_PCT = 0.10       # Crypto.com taker fee per side (0.1%)
ROUND_TRIP_FEE_PCT = 0.20     # Buy + sell combined fees
MIN_PROFITABLE_SPREAD = 0.25  # Minimum spread to overcome fees + slippage

# --- Position dicts (protected by locks above) ---
positions = {}
futures_positions = {}
scalp_positions = {}

# --- CEX exchange objects (set via attach functions) ---
exchange = None
exchange2 = None
exchange_margin = None
exchange_futures = None

# --- Wallet / DEX references (set via attach functions) ---
wallets = None
dex = None
known_pairs = set()

# --- Chain state ---
hot_chain = "SOL"

# --- Cooldown tracking ---
_cooldown_map = {}

# --- Recently sold tracking (prevents instant re-buy) ---
recently_sold = {}

# --- Stale position config ---
STALE_CONFIG = {
    "max_age_hours": 24,
    "min_pnl_pct": -1.0,
    "check_interval": 300,
}

# --- Trend cache (used by get_trend_filter) ---
_trend_cache = {"trend": "neutral", "ts": 0}


# =========================================================================
# 7. STRATEGY STATS AND KELLY FRACTION
# =========================================================================
_strategy_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "total_pnl": 0.0})


def update_strategy_stats(strategy: str, pnl: float):
    with stats_lock:
        s = _strategy_stats[strategy]
        if pnl >= 0:
            s["wins"] += 1
        else:
            s["losses"] += 1
        s["total_pnl"] += pnl


def get_kelly_fraction(strategy: str) -> float:
    """
    Half-Kelly sizing.  Returns fraction in [0.05, 0.5].
    If insufficient data, returns default 0.15.
    """
    with stats_lock:
        s = _strategy_stats[strategy]
        total = s["wins"] + s["losses"]
        if total < 5:
            return 0.35
        win_rate = s["wins"] / total
        if win_rate <= 0 or win_rate >= 1:
            return 0.15
        avg_win = max(s["total_pnl"] / s["wins"], 0.001) if s["wins"] else 1
        avg_loss = max(-s["total_pnl"] / s["losses"], 0.001) if s["losses"] else 1
        b = avg_win / avg_loss
        kelly = win_rate - (1 - win_rate) / b
        half_kelly = kelly / 2
        return max(0.05, min(0.5, half_kelly))


def calculate_pnl_after_fees(entry_price: float, exit_price: float,
                              qty: float, direction: str = "long") -> tuple:
    """
    Calculate PnL after exchange fees.
    Returns (pnl_pct, pnl_usd, fee_usd).
    direction: 'long' or 'short'
    """
    if entry_price <= 0:
        return 0.0, 0.0, 0.0

    if direction == "short":
        raw_pnl_pct = (entry_price - exit_price) / entry_price * 100
    else:
        raw_pnl_pct = (exit_price - entry_price) / entry_price * 100

    pnl_pct = raw_pnl_pct - ROUND_TRIP_FEE_PCT
    trade_value = qty * exit_price
    fee_usd = trade_value * ROUND_TRIP_FEE_PCT / 100
    pnl_usd = pnl_pct / 100 * trade_value

    return round(pnl_pct, 4), round(pnl_usd, 4), round(fee_usd, 4)


# =========================================================================
# 8. ATTACH FUNCTIONS — NO DUPLICATES
# =========================================================================
def attach_wallets(w):
    """Attach the EmpireWallets instance."""
    global wallets
    wallets = w
    log("[CORE] Wallets attached")


def attach_dex(d):
    """Attach the EmpireDEXCore instance."""
    global dex
    dex = d
    log("[CORE] DEX attached")


def set_known_pairs(pairs: set):
    """Set the known CEX trading pairs."""
    global known_pairs
    known_pairs = pairs
    log(f"[CORE] {len(known_pairs)} known pairs loaded")


def set_cex_clients(
    main=None, secondary=None, margin=None, futures=None
):
    """Attach one or more ccxt exchange instances."""
    global exchange, exchange2, exchange_margin, exchange_futures
    if main is not None:
        exchange = main
    if secondary is not None:
        exchange2 = secondary
    if margin is not None:
        exchange_margin = margin
    if futures is not None:
        exchange_futures = futures
    log("[CORE] CEX clients attached")


# =========================================================================
# 9. SAFE-CALL HELPERS AND TICKER HELPERS
# =========================================================================
def safe_call(fn, *args, retries=2, delay=1.0, **kwargs):
    """Call fn with retries.  Returns result or None on failure."""
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt < retries:
                time.sleep(delay)
            else:
                return None


def safe_fetch_ticker(symbol: str) -> dict:
    """Fetch ticker with fallback to exchange2.  Returns dict or empty."""
    for ex in [exchange, exchange2]:
        if not ex:
            continue
        try:
            t = ex.fetch_ticker(symbol)
            if t and t.get("last") and t["last"] > 0:
                return t
        except Exception:
            continue
    return {}


def safe_fetch_order_book(symbol: str, limit: int = 5) -> dict:
    """Fetch order book with fallback."""
    for ex in [exchange, exchange2]:
        if not ex:
            continue
        try:
            ob = ex.fetch_order_book(symbol, limit)
            if ob and ob.get("bids") and ob.get("asks"):
                return ob
        except Exception:
            continue
    return {}


def get_24h_volume(symbol: str) -> float:
    """Return 24h quote volume for symbol, or 0."""
    t = safe_fetch_ticker(symbol)
    if not t:
        return 0.0
    return float(t.get("quoteVolume", 0) or 0)


def get_spread_pct(symbol: str) -> float:
    """Return bid-ask spread as pct.  High spread = illiquid."""
    ob = safe_fetch_order_book(symbol, 5)
    if not ob or not ob.get("bids") or not ob.get("asks"):
        return 999.0
    best_bid = ob["bids"][0][0]
    best_ask = ob["asks"][0][0]
    if best_bid <= 0:
        return 999.0
    return (best_ask - best_bid) / best_bid * 100


# =========================================================================
# 10. BALANCE AND CAPITAL FUNCTIONS
# =========================================================================
def get_total_balance_usd() -> float:
    """Fetch total USDT balance from the main exchange."""
    if not exchange:
        return 0.0
    try:
        bal = safe_call(exchange.fetch_balance)
        if not bal:
            return 0.0
        usdt = float(bal.get("total", {}).get("USDT", 0))
        return usdt
    except Exception:
        return 0.0


def get_free_usdt() -> float:
    """Fetch free (available) USDT."""
    if not exchange:
        return 0.0
    try:
        bal = safe_call(exchange.fetch_balance)
        if not bal:
            return 0.0
        return float(bal.get("free", {}).get("USDT", 0))
    except Exception:
        return 0.0


def get_trade_capital(strategy: str = "default", symbol: str = None) -> float:
    """
    Compute how much USD to use for a single trade.
    Uses Kelly fraction scaled by WORKER_CAPITAL_FRACTION and TRADE_SIZE_FRACTION.
    Clamped to [MIN_TRADE_SIZE_USD, MAX_TRADE_SIZE_USD].
    """
    with capital_lock:
        free = get_free_usdt()
        kelly = get_kelly_fraction(strategy)
        raw = free * WORKER_CAPITAL_FRACTION * TRADE_SIZE_FRACTION * kelly

        # AI confidence scaling
        ai_multiplier = 1.0
        if AI and exchange and symbol:
            try:
                ai = AI.get_ai_score(exchange, symbol)
                ai_multiplier = ai["score"] / 100.0
                ai_multiplier = max(0.3, min(1.5, ai_multiplier))
                fg_mult = AI.get_fear_greed_multiplier()
                ai_multiplier *= fg_mult
            except Exception:
                ai_multiplier = 1.0
        raw *= ai_multiplier

        clamped = max(MIN_TRADE_SIZE_USD, min(MAX_TRADE_SIZE_USD, raw))
        return round(clamped, 2)


# =========================================================================
# 11. DB LOGGING
# =========================================================================
DB_FILE = "empire_trades.db"


def _init_db():
    """Create the trades table if it does not exist."""
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT,
                    action TEXT,
                    symbol TEXT,
                    amount REAL,
                    price REAL,
                    pnl_pct REAL,
                    pos_type TEXT,
                    strategy TEXT,
                    notes TEXT
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DB] init error: {e}")


_init_db()


def _migrate_db():
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.execute("PRAGMA table_info(trades)")
            columns = [row[1] for row in cursor.fetchall()]
            if "fee_usd" not in columns:
                conn.execute("ALTER TABLE trades ADD COLUMN fee_usd REAL DEFAULT 0.0")
            if "pnl_net_usd" not in columns:
                conn.execute("ALTER TABLE trades ADD COLUMN pnl_net_usd REAL DEFAULT 0.0")
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DB] migration: {e}")


_migrate_db()


def log_trade(
    action: str,
    symbol: str,
    amount: float,
    price: float,
    pnl_pct: float = 0.0,
    pos_type: str = "spot",
    strategy: str = "",
    notes: str = "",
    fee_usd: float = 0.0,
    pnl_net_usd: float = 0.0,
):
    """Insert a trade record into the DB."""
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.execute(
                "INSERT INTO trades (ts, action, symbol, amount, price, pnl_pct, pos_type, strategy, notes, fee_usd, pnl_net_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now().isoformat(),
                    action,
                    symbol,
                    amount,
                    price,
                    pnl_pct,
                    pos_type,
                    strategy,
                    notes,
                    fee_usd,
                    pnl_net_usd,
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[DB] log_trade error: {e}")


def get_trade_history(limit: int = 50) -> list:
    """Return recent trade records."""
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            cur = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            )
            rows = cur.fetchall()
            conn.close()
            return rows
        except Exception:
            return []


# =========================================================================
# 12. TREND FILTER (GET_TREND_FILTER — WAS MISSING, CAUSED CRASHES)
# =========================================================================
def get_trend_filter() -> str:
    """
    BTC 1h trend: 'up', 'down', or 'neutral'.
    Cached for 5 minutes to avoid excessive API calls.
    Used by ~8 workers to gate entries during downtrends.
    """
    global _trend_cache
    now = time.time()
    if now - _trend_cache["ts"] < 300:
        return _trend_cache["trend"]

    # Use AI multi-indicator trend if available
    if AI and exchange:
        try:
            trend = AI.get_ai_trend(exchange)
            _trend_cache = {"trend": trend, "ts": now}
            return trend
        except Exception:
            pass

    # Fallback: original simple logic
    try:
        if not exchange:
            return "neutral"
        ohlcv = exchange.fetch_ohlcv("BTC/USDT", "1h", limit=3)
        if not ohlcv or len(ohlcv) < 3:
            return "neutral"
        close_prev = ohlcv[-2][4]
        close_now = ohlcv[-1][4]
        change = (close_now - close_prev) / close_prev * 100
        if change > 2.5:
            trend = "up"
        elif change < -2.5:
            trend = "down"
        else:
            trend = "neutral"
        _trend_cache = {"trend": trend, "ts": now}
        return trend
    except Exception:
        return "neutral"


# =========================================================================
# 13. POSITION MANAGEMENT HELPERS
# =========================================================================
def can_trade_symbol(symbol: str) -> bool:
    """
    Check if we are allowed to open a new position for this symbol.
    Blocks if:
      - Already holding the symbol
      - At max concurrent positions
      - In cooldown period
      - Recently sold (anti-churn)
    """
    # Already holding?
    with positions_lock:
        if symbol in positions:
            return False
        if len(positions) >= MAX_CONCURRENT_POSITIONS:
            return False

    # Cooldown check
    now = time.time()
    last_trade = _cooldown_map.get(symbol, 0)
    if now - last_trade < COOLDOWN_SECONDS:
        return False

    # Recently sold check (prevent instant re-buy)
    sold_at = recently_sold.get(symbol, 0)
    if now - sold_at < COOLDOWN_SECONDS:
        return False

    return True


def mark_traded(symbol: str):
    """Record that we just traded this symbol (for cooldown)."""
    _cooldown_map[symbol] = time.time()


def rotate_for_better_opportunity(new_symbol: str, new_score: float) -> bool:
    """
    If we are at max positions, check if the weakest existing position
    can be rotated out for a better opportunity.
    Returns True if a slot was freed.
    """
    with positions_lock:
        if len(positions) < MAX_CONCURRENT_POSITIONS:
            return True  # slot available, no rotation needed

        # Find weakest position by current PnL
        worst_sym = None
        worst_pnl = float("inf")
        for sym, pos in positions.items():
            entry = pos.get("entry", 0)
            if entry <= 0:
                continue
            t = safe_fetch_ticker(sym)
            if not t or not t.get("last"):
                continue
            pnl = (t["last"] - entry) / entry * 100
            if pnl < worst_pnl:
                worst_pnl = pnl
                worst_sym = sym

        # Only rotate if worst position is negative and new score is high
        if worst_sym and worst_pnl < -1.0 and new_score > 70:
            # Sell the worst position
            try:
                coin = worst_sym.split("/")[0]
                bal = safe_call(exchange.fetch_balance)
                if bal:
                    free_amt = float(bal.get("free", {}).get(coin, 0))
                    if free_amt > 0:
                        exchange.create_market_sell_order(worst_sym, free_amt)
                        t = safe_fetch_ticker(worst_sym)
                        price = t.get("last", 0) if t else 0
                        msg = f"[ROTATE] Sold {worst_sym} (PnL {worst_pnl:.1f}%) for {new_symbol}"
                        print(msg)
                        log(msg)
                        log_trade("ROTATE_SELL", worst_sym, free_amt, price, worst_pnl, "spot")
                        record_trade_result(worst_sym, worst_pnl * free_amt * price / 100, worst_pnl)
                positions.pop(worst_sym, None)
                recently_sold[worst_sym] = time.time()
                return True
            except Exception as e:
                print(f"[ROTATE] Failed to sell {worst_sym}: {e}")
                return False
        return False


# =========================================================================
# 14. CEX ORDER HELPERS
# =========================================================================

# ---- SPOT BUY ----
def spot_buy(symbol: str, usd_amount: float, strategy: str = "unknown") -> bool:
    """
    Market buy on spot.  Returns True if order placed successfully.
    """
    if not exchange:
        log(f"[SPOT BUY] No exchange — skipping {symbol}")
        return False

    # AI signal gate (additive — skip if AI unavailable)
    if AI and strategy not in ("new_listing", "synced"):
        try:
            ai = AI.get_ai_score(exchange, symbol)
            if ai["score"] < 40:
                log(f"[AI GATE] {symbol} score {ai['score']:.0f} < 40 -> BLOCKED")
                return False
        except Exception:
            pass

    try:
        ticker = safe_fetch_ticker(symbol)
        if not ticker or not ticker.get("last") or ticker["last"] <= 0:
            log(f"[SPOT BUY] No valid ticker for {symbol}")
            return False

        price = ticker["last"]
        qty = usd_amount / price

        # Respect exchange minimum order size
        markets = exchange.load_markets()
        if symbol in markets:
            min_amt = markets[symbol].get("limits", {}).get("amount", {}).get("min", 0)
            if min_amt and qty < min_amt:
                log(f"[SPOT BUY] {symbol} qty {qty} below minimum {min_amt}")
                return False

        order = exchange.create_market_buy_order(symbol, qty)
        filled_price = order.get("average", price)
        filled_qty = order.get("filled", qty)

        with positions_lock:
            positions[symbol] = {
                "entry": filled_price,
                "qty": filled_qty,
                "usd": usd_amount,
                "time": time.time(),
                "strategy": strategy,
                "type": "spot",
                "high": filled_price,
            }

        mark_traded(symbol)
        msg = f"[SPOT BUY] {symbol} @ ${filled_price:.6f} qty={filled_qty:.4f} (${usd_amount:.2f}) [{strategy}]"
        print(msg)
        log(msg)
        log_trade("BUY", symbol, filled_qty, filled_price, 0.0, "spot", strategy)
        return True

    except Exception as e:
        log(f"[SPOT BUY ERROR] {symbol}: {e}")
        return False


# ---- SPOT SELL ----
def spot_sell(symbol: str, reason: str = "take_profit", fraction: float = 1.0) -> bool:
    """
    Market sell on spot.  Simplified — always executes the sell regardless
    of PnL so that stop-loss and rotation sells work properly.
    fraction: 0.0-1.0 of held amount to sell.
    """
    if not exchange:
        return False
    try:
        coin = symbol.split("/")[0]
        bal = safe_call(exchange.fetch_balance)
        if not bal:
            return False
        free_amt = float(bal.get("free", {}).get(coin, 0))
        if free_amt <= 0.0001:
            # Nothing to sell — clean up position record
            with positions_lock:
                positions.pop(symbol, None)
            return False

        sell_qty = free_amt * fraction
        if sell_qty <= 0:
            return False

        ticker = safe_fetch_ticker(symbol)
        price = ticker.get("last", 0) if ticker else 0

        order = exchange.create_market_sell_order(symbol, sell_qty)
        filled_price = order.get("average", price)

        # Calculate PnL
        pnl_pct = 0.0
        pnl_usd = 0.0
        fee_usd = 0.0
        with positions_lock:
            pos = positions.get(symbol, {})
            entry = pos.get("entry", filled_price)
            if entry > 0:
                pnl_pct, pnl_usd, fee_usd = calculate_pnl_after_fees(entry, filled_price, sell_qty, "long")

            if fraction >= 1.0:
                positions.pop(symbol, None)
            else:
                if symbol in positions:
                    positions[symbol]["qty"] = positions[symbol].get("qty", 0) - sell_qty

        recently_sold[symbol] = time.time()
        record_trade_result(symbol, pnl_usd, pnl_pct, fee_usd=fee_usd)

        strategy = pos.get("strategy", "unknown") if pos else "unknown"
        update_strategy_stats(strategy, pnl_usd)

        msg = f"[SPOT SELL] {symbol} @ ${filled_price:.6f} qty={sell_qty:.4f} PnL={pnl_pct:+.2f}% [${pnl_usd:+.2f}] reason={reason}"
        print(msg)
        log(msg)
        log_trade("SELL", symbol, sell_qty, filled_price, pnl_pct, "spot", strategy, reason, fee_usd=fee_usd, pnl_net_usd=pnl_usd)
        return True

    except Exception as e:
        log(f"[SPOT SELL ERROR] {symbol}: {e}")
        return False


# ---- MARGIN BUY (LONG) ----
def margin_buy(symbol: str, usd_amount: float, strategy: str = "margin") -> bool:
    """Open a margin long position."""
    ex = exchange_margin or exchange
    if not ex:
        return False
    try:
        # Check margin position count
        with positions_lock:
            margin_count = sum(
                1 for p in positions.values() if "margin" in p.get("type", "")
            )
            if margin_count >= MAX_MARGIN_POSITIONS:
                log(f"[MARGIN BUY] At max margin positions ({MAX_MARGIN_POSITIONS})")
                return False

        ticker = safe_fetch_ticker(symbol)
        if not ticker or not ticker.get("last"):
            return False
        price = ticker["last"]
        qty = usd_amount / price

        order = ex.create_market_buy_order(symbol, qty, {"marginMode": "cross"})
        filled_price = order.get("average", price)
        filled_qty = order.get("filled", qty)

        with positions_lock:
            positions[symbol] = {
                "entry": filled_price,
                "qty": filled_qty,
                "usd": usd_amount,
                "time": time.time(),
                "strategy": strategy,
                "type": "margin_long",
                "high": filled_price,
            }

        mark_traded(symbol)
        msg = f"[MARGIN BUY] {symbol} @ ${filled_price:.6f} qty={filled_qty:.4f} [{strategy}]"
        print(msg)
        log(msg)
        log_trade("MARGIN_BUY", symbol, filled_qty, filled_price, 0.0, "margin_long", strategy)
        return True

    except Exception as e:
        log(f"[MARGIN BUY ERROR] {symbol}: {e}")
        return False


# ---- MARGIN SELL (SHORT ENTRY) ----
def margin_sell(symbol: str, usd_amount: float, strategy: str = "margin_short") -> bool:
    """Open a margin short position."""
    ex = exchange_margin or exchange
    if not ex:
        return False
    try:
        with positions_lock:
            margin_count = sum(
                1 for p in positions.values() if "margin" in p.get("type", "")
            )
            if margin_count >= MAX_MARGIN_POSITIONS:
                log(f"[MARGIN SELL] At max margin positions ({MAX_MARGIN_POSITIONS})")
                return False

        ticker = safe_fetch_ticker(symbol)
        if not ticker or not ticker.get("last"):
            return False
        price = ticker["last"]
        qty = usd_amount / price

        order = ex.create_market_sell_order(symbol, qty, {"marginMode": "cross"})
        filled_price = order.get("average", price)
        filled_qty = order.get("filled", qty)

        with positions_lock:
            positions[symbol] = {
                "entry": filled_price,
                "qty": filled_qty,
                "usd": usd_amount,
                "time": time.time(),
                "strategy": strategy,
                "type": "margin_short",
                "high": filled_price,
            }

        mark_traded(symbol)
        msg = f"[MARGIN SHORT] {symbol} @ ${filled_price:.6f} qty={filled_qty:.4f} [{strategy}]"
        print(msg)
        log(msg)
        log_trade("MARGIN_SHORT", symbol, filled_qty, filled_price, 0.0, "margin_short", strategy)
        return True

    except Exception as e:
        log(f"[MARGIN SHORT ERROR] {symbol}: {e}")
        return False


# ---- MARGIN CLOSE ----
def margin_close(symbol: str, reason: str = "take_profit") -> bool:
    """Close a margin position (long or short)."""
    ex = exchange_margin or exchange
    if not ex:
        return False
    try:
        with positions_lock:
            pos = positions.get(symbol)
            if not pos:
                return False
            pos_type = pos.get("type", "margin_long")
            entry = pos.get("entry", 0)
            qty = pos.get("qty", 0)

        ticker = safe_fetch_ticker(symbol)
        price = ticker.get("last", 0) if ticker else 0

        if pos_type == "margin_short":
            # Close short by buying
            order = ex.create_market_buy_order(symbol, qty, {"marginMode": "cross"})
        else:
            # Close long by selling
            order = ex.create_market_sell_order(symbol, qty, {"marginMode": "cross"})

        filled_price = order.get("average", price)
        direction = "short" if pos_type == "margin_short" else "long"
        pnl_pct, pnl_usd, fee_usd = calculate_pnl_after_fees(entry, filled_price, qty, direction)

        with positions_lock:
            positions.pop(symbol, None)

        recently_sold[symbol] = time.time()
        record_trade_result(symbol, pnl_usd, pnl_pct, fee_usd=fee_usd)
        strategy = pos.get("strategy", "margin")
        update_strategy_stats(strategy, pnl_usd)

        msg = f"[MARGIN CLOSE] {symbol} PnL={pnl_pct:+.2f}% [${pnl_usd:+.2f}] reason={reason}"
        print(msg)
        log(msg)
        log_trade("MARGIN_CLOSE", symbol, qty, filled_price, pnl_pct, pos_type, strategy, reason, fee_usd=fee_usd, pnl_net_usd=pnl_usd)
        return True

    except Exception as e:
        log(f"[MARGIN CLOSE ERROR] {symbol}: {e}")
        return False


# ---- FUTURES ORDER ----
def futures_order(
    symbol: str,
    side: str,
    usd_amount: float,
    leverage: int = 5,
    strategy: str = "futures",
) -> bool:
    """Open or close a futures position."""
    ex = exchange_futures or exchange
    if not ex:
        return False
    try:
        ticker = safe_fetch_ticker(symbol)
        if not ticker or not ticker.get("last"):
            return False
        price = ticker["last"]
        qty = usd_amount / price

        # Set leverage
        try:
            ex.set_leverage(leverage, symbol)
        except Exception:
            pass  # Some exchanges don't support per-symbol leverage setting

        params = {"type": "future"}
        if side == "buy":
            order = ex.create_market_buy_order(symbol, qty, params)
        else:
            order = ex.create_market_sell_order(symbol, qty, params)

        filled_price = order.get("average", price)
        filled_qty = order.get("filled", qty)

        with futures_lock:
            if side == "buy":
                futures_positions[symbol] = {
                    "entry": filled_price,
                    "qty": filled_qty,
                    "side": "long",
                    "leverage": leverage,
                    "usd": usd_amount,
                    "time": time.time(),
                    "strategy": strategy,
                }
            else:
                # Closing or opening short
                if symbol in futures_positions:
                    pos = futures_positions.pop(symbol)
                    pnl_pct = (filled_price - pos["entry"]) / pos["entry"] * 100
                    if pos.get("side") == "short":
                        pnl_pct = -pnl_pct
                    pnl_usd = pnl_pct / 100 * filled_qty * filled_price
                    record_trade_result(symbol, pnl_usd, pnl_pct)
                    update_strategy_stats(strategy, pnl_usd)
                else:
                    futures_positions[symbol] = {
                        "entry": filled_price,
                        "qty": filled_qty,
                        "side": "short",
                        "leverage": leverage,
                        "usd": usd_amount,
                        "time": time.time(),
                        "strategy": strategy,
                    }

        msg = f"[FUTURES] {side.upper()} {symbol} @ ${filled_price:.6f} x{leverage} [{strategy}]"
        print(msg)
        log(msg)
        log_trade(f"FUTURES_{side.upper()}", symbol, filled_qty, filled_price, 0.0, "futures", strategy)
        return True

    except Exception as e:
        log(f"[FUTURES ERROR] {symbol} {side}: {e}")
        return False


# =========================================================================
# 15. DYNAMIC TRAILING EXIT
# =========================================================================
def dynamic_trailing_exit(symbol: str, current_price: float) -> bool:
    """
    Adaptive trailing stop.  Tightens as profit grows.
    Returns True if the position should be exited.

      Profit band      Trail %
      < 2%             no trail (hold)
      2-5%             1.5% trail
      5-10%            2.0% trail
      10-20%           3.0% trail
      > 20%            4.0% trail
    """
    with positions_lock:
        pos = positions.get(symbol)
        if not pos:
            return False
        entry = pos.get("entry", 0)
        high = pos.get("high", current_price)

        if entry <= 0:
            return False

        # Update high water mark
        if current_price > high:
            positions[symbol]["high"] = current_price
            high = current_price

        pnl_pct = (current_price - entry) / entry * 100
        drop_from_high = (high - current_price) / high * 100

        # Determine trail threshold
        if pnl_pct < 2.0:
            return False  # not enough profit to trail
        elif pnl_pct < 5.0:
            trail = 1.5
        elif pnl_pct < 10.0:
            trail = 2.0
        elif pnl_pct < 20.0:
            trail = 3.0
        else:
            trail = 4.0

        if drop_from_high >= trail:
            return True

    return False


# =========================================================================
# 16. STALE_CONFIG dict — already defined in section 6
#     (Kept as comment reference; the dict is in the global config block)
# =========================================================================

# =========================================================================
# 17. recently_sold dict — already defined in section 6
#     (Kept as comment reference; the dict is in the global config block)
# =========================================================================


# =========================================================================
# STOP LOSS WORKER
# =========================================================================
def stop_loss_worker():
    """Hard stop loss: -3% on spot, -5% on margin. Runs every 5s."""
    print("[STOP LOSS] Hard stop loss monitor active")
    log("[STOP LOSS] Ready! -3% spot / -5% margin")
    while True:
        try:
            if not exchange:
                time.sleep(5)
                continue
            with positions_lock:
                pos_copy = dict(positions)
            for sym, pos in pos_copy.items():
                try:
                    ticker = safe_fetch_ticker(sym)
                    if not ticker or not ticker.get("last"):
                        continue
                    price = ticker["last"]
                    if price <= 0:
                        continue
                    entry = pos.get("entry", price)
                    pos_type = pos.get("type", "spot")
                    direction = "short" if "margin" in pos_type else "long"
                    pnl_pct, _, _ = calculate_pnl_after_fees(entry, price, 1.0, direction)

                    threshold = -5.0 if "margin" in pos_type else -3.0

                    if pnl_pct <= threshold:
                        coin = sym.split("/")[0]
                        bal = safe_call(exchange.fetch_balance)
                        if bal:
                            free_amt = float(bal.get("free", {}).get(coin, 0))
                            if free_amt > 0.0001:
                                exchange.create_market_sell_order(sym, free_amt)
                                msg = f"[STOP LOSS] {sym} {pnl_pct:.1f}% -> SOLD {free_amt}"
                                print(msg)
                                log(msg)
                                log_trade(
                                    "STOP_LOSS", sym, free_amt, price,
                                    pnl_pct, pos_type,
                                )
                                record_trade_result(sym, pnl_pct / 100 * free_amt * price, pnl_pct)
                        with positions_lock:
                            positions.pop(sym, None)
                        recently_sold[sym] = time.time()
                except Exception:
                    continue
            time.sleep(5)
        except Exception as e:
            print(f"[STOP LOSS ERROR] {e}")
            time.sleep(5)


# =========================================================================
# AUTO-RESTART THREAD WRAPPER
# =========================================================================
def resilient_thread(target, name=None):
    """Wraps a worker in auto-restart logic. If it crashes, waits 10s and restarts."""
    def wrapper():
        while True:
            try:
                target()
            except Exception as e:
                print(f"[CRASH] {name or target.__name__} crashed: {e}")
                log(f"[CRASH] {name or target.__name__}: {e}")
                time.sleep(10)
    return wrapper


# =========================================================================
# WORKERS
# =========================================================================

# --- Shared state for workers ---
HOT_PAIRS = []
CORE_PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
    "ADA/USDT", "DOGE/USDT", "MATIC/USDT", "DOT/USDT", "AVAX/USDT",
    "LINK/USDT", "UNI/USDT", "ATOM/USDT", "LTC/USDT", "BCH/USDT",
    "SHIB/USDT", "PEPE/USDT", "FET/USDT", "INJ/USDT", "TIA/USDT",
    "ARB/USDT", "OP/USDT", "SUI/USDT", "SEI/USDT", "APT/USDT",
    "NEAR/USDT", "FIL/USDT", "RENDER/USDT", "WIF/USDT", "BONK/USDT",
    "JUP/USDT", "ONDO/USDT", "CRO/USDT", "ALGO/USDT", "MANA/USDT",
    "SAND/USDT", "GALA/USDT", "IMX/USDT", "GRT/USDT", "AAVE/USDT",
]
SEEN_PAIRS = set()
SEEN_PAIRS_FILE = "seen_new_listings.json"


def _load_seen_pairs():
    global SEEN_PAIRS
    if os.path.exists(SEEN_PAIRS_FILE):
        try:
            with open(SEEN_PAIRS_FILE, "r") as f:
                SEEN_PAIRS = set(json.load(f))
            log(f"[CORE] Loaded {len(SEEN_PAIRS)} previously seen pairs")
        except Exception:
            SEEN_PAIRS = set()


def _save_seen_pairs():
    try:
        with open(SEEN_PAIRS_FILE, "w") as f:
            json.dump(list(SEEN_PAIRS), f)
    except Exception:
        pass


_load_seen_pairs()


# =========================================================================
# WORKER: Hot Token Scanner
# =========================================================================
def hot_token_scanner_worker():
    global HOT_PAIRS, SEEN_PAIRS
    log("[HOT SCANNER] Starting...")
    time.sleep(5)

    HOT_PAIRS = list(CORE_PAIRS)
    last_seen_symbols = set()

    while True:
        try:
            if not exchange:
                time.sleep(10)
                continue

            symbols = set(exchange.symbols or [])
            usdt_symbols = {s for s in symbols if s.endswith("/USDT")}

            if not last_seen_symbols:
                last_seen_symbols = usdt_symbols
                time.sleep(30)
                continue

            # Detect new listings
            new_listings = usdt_symbols - last_seen_symbols
            for sym in new_listings:
                if sym in HOT_PAIRS:
                    continue
                try:
                    ticker = safe_fetch_ticker(sym)
                    if not ticker or ticker["last"] <= 0:
                        continue
                    vol = ticker.get("quoteVolume", 0) or 0
                    if vol < 10_000:
                        continue
                    SEEN_PAIRS.add(sym)
                    HOT_PAIRS.append(sym)
                    log(f"[NEW LISTING] {sym} added to HOT_PAIRS")
                except Exception:
                    continue

            last_seen_symbols = usdt_symbols

            # Volume leaders
            volume_leaders = []
            for sym in list(usdt_symbols)[:100]:
                try:
                    ticker = safe_fetch_ticker(sym)
                    vol = ticker.get("quoteVolume", 0) or 0
                    if vol < 25_000:
                        continue
                    volume_leaders.append((sym, vol))
                except Exception:
                    continue

            volume_leaders.sort(key=lambda x: x[1], reverse=True)
            top_by_volume = [s for s, _ in volume_leaders[:40]]

            merged = set(CORE_PAIRS) | set(top_by_volume) | set(HOT_PAIRS)
            HOT_PAIRS = list(merged)

            # Prune dead tokens
            for sym in list(HOT_PAIRS):
                if sym in CORE_PAIRS:
                    continue
                try:
                    ticker = safe_fetch_ticker(sym)
                    vol = ticker.get("quoteVolume", 0) or 0
                    if vol < 5_000:
                        HOT_PAIRS.remove(sym)
                except Exception:
                    continue

            log(f"[HOT SCANNER] Monitoring {len(HOT_PAIRS)} pairs")
            _save_seen_pairs()
            time.sleep(60)

        except Exception as e:
            log(f"[HOT SCANNER ERROR] {e}")
            time.sleep(30)


# =========================================================================
# WORKER: New Listing Sniper
# =========================================================================
def new_listing_sniper_worker():
    log("[SNIPER] New CEX listing sniper active")
    time.sleep(8)

    sniped = {}

    while True:
        try:
            if not exchange:
                time.sleep(5)
                continue

            current_symbols = set(exchange.symbols or [])
            new_symbols = {s for s in current_symbols
                          if s.endswith("/USDT") and s not in SEEN_PAIRS}

            for sym in new_symbols:
                try:
                    ticker = safe_fetch_ticker(sym)
                    price = ticker.get("last", 0) or 0
                    volume = ticker.get("quoteVolume", 0) or 0

                    if price <= 0 or volume < 20_000:
                        SEEN_PAIRS.add(sym)
                        _save_seen_pairs()
                        continue

                    trend = get_trend_filter()
                    if trend == "down" and volume < 100_000:
                        SEEN_PAIRS.add(sym)
                        _save_seen_pairs()
                        continue

                    if not can_trade_symbol(sym):
                        SEEN_PAIRS.add(sym)
                        _save_seen_pairs()
                        continue

                    capital = max(10.0, get_trade_capital("sniper") * 1.5)

                    msg = f"[SNIPER] NEW LISTING: {sym} @ ${price:.8f} vol ${volume:,.0f} -> ${capital:.2f}"
                    log(msg)

                    if spot_buy(sym, capital, "new_listing"):
                        sniped[sym] = {
                            "entry": price, "ts": time.time(), "peak": price,
                        }

                    SEEN_PAIRS.add(sym)
                    _save_seen_pairs()
                    time.sleep(1.5)

                except Exception as e:
                    SEEN_PAIRS.add(sym)
                    _save_seen_pairs()

            # Manage sniped positions
            for sym, pos in list(sniped.items()):
                try:
                    ticker = safe_fetch_ticker(sym)
                    price = ticker["last"]
                    entry = pos["entry"]
                    pnl_pct = (price - entry) / entry * 100

                    if price > pos["peak"]:
                        pos["peak"] = price

                    should_exit = False
                    reason = ""

                    if pnl_pct >= 25:
                        should_exit = True
                        reason = f"+{pnl_pct:.1f}% TP"
                    elif pnl_pct <= -8:
                        should_exit = True
                        reason = f"{pnl_pct:.1f}% SL"
                    elif time.time() - pos["ts"] > 300:
                        drop = (pos["peak"] - price) / pos["peak"] * 100
                        if drop > 15:
                            should_exit = True
                            reason = f"fade -{drop:.1f}%"

                    if should_exit:
                        log(f"[SNIPER EXIT] {sym} {reason}")
                        spot_sell(sym, reason="stop_loss")
                        del sniped[sym]

                except Exception:
                    continue

            time.sleep(2)

        except Exception as e:
            log(f"[SNIPER ERROR] {e}")
            time.sleep(5)


# =========================================================================
# WORKER: Volume Surge Detector
# =========================================================================
def volume_surge_worker():
    log("[VOLUME SURGE] Volume spike detector active")
    time.sleep(8)

    volume_history = {}

    while True:
        try:
            if not exchange:
                time.sleep(5)
                continue

            scan_pairs = list(HOT_PAIRS if HOT_PAIRS else CORE_PAIRS)
            random.shuffle(scan_pairs)

            for sym in scan_pairs:
                try:
                    ticker = safe_fetch_ticker(sym)
                    vol = ticker.get("quoteVolume", 0) or 0
                    if vol < 15_000:
                        continue

                    if sym not in volume_history:
                        volume_history[sym] = []

                    volume_history[sym].append(vol)
                    if len(volume_history[sym]) > 12:
                        volume_history[sym].pop(0)

                    if len(volume_history[sym]) < 6:
                        continue

                    avg_vol = sum(volume_history[sym]) / len(volume_history[sym])
                    surge = vol / max(avg_vol, 1)

                    if surge >= 2.0:
                        if not can_trade_symbol(sym):
                            continue

                        capital = get_trade_capital("spot")
                        if capital < 5:
                            continue

                        log(f"[VOLUME SURGE] {sym} {surge:.1f}x avg -> ${capital:.2f}")
                        spot_buy(sym, capital, "volume_surge")
                        volume_history[sym] = [vol]
                        time.sleep(2)

                except Exception:
                    continue

            time.sleep(10)

        except Exception as e:
            log(f"[VOLUME SURGE ERROR] {e}")
            time.sleep(5)


# =========================================================================
# WORKER: Breakout Scanner
# =========================================================================
def breakout_worker():
    log("[BREAKOUT] 24h high/low scanner active")
    time.sleep(8)

    while True:
        try:
            if not exchange:
                time.sleep(5)
                continue

            markets = list(set(s for s in (HOT_PAIRS or []) + CORE_PAIRS
                       if s.endswith("/USDT")))
            random.shuffle(markets)
            markets = markets[:40]

            for sym in markets:
                try:
                    ticker = safe_fetch_ticker(sym)
                    price = ticker.get("last", 0) or 0
                    high_24h = ticker.get("high", 0) or 0
                    low_24h = ticker.get("low", 0) or 0
                    vol = ticker.get("quoteVolume", 0) or 0

                    if price <= 0 or high_24h <= 0 or vol < 50_000:
                        continue

                    # Breakout long
                    if high_24h > 0:
                        breakout_pct = (price - high_24h) / high_24h * 100
                        if breakout_pct >= 0.4:
                            if get_trend_filter() == "down":
                                continue
                            if not can_trade_symbol(sym):
                                continue
                            capital = get_trade_capital("spot") * 0.7
                            if capital > 8:
                                log(f"[BREAKOUT LONG] {sym} +{breakout_pct:.2f}%")
                                spot_buy(sym, capital, "breakout")
                                time.sleep(1.2)

                    # Breakdown short
                    if low_24h > 0:
                        breakdown_pct = (low_24h - price) / low_24h * 100
                        if breakdown_pct >= 0.4:
                            if get_trend_filter() == "up":
                                continue
                            capital = get_trade_capital("margin") * 0.6
                            if capital > 8:
                                log(f"[BREAKDOWN SHORT] {sym} -{breakdown_pct:.2f}%")
                                margin_sell(sym, capital * 4, "breakdown_short")
                                time.sleep(1.2)

                except Exception:
                    continue

            time.sleep(15)

        except Exception as e:
            log(f"[BREAKOUT ERROR] {e}")
            time.sleep(5)


# =========================================================================
# WORKER: Momentum Scalper
# =========================================================================
SCALP_CONFIG = {
    "PROFIT_TARGET_1": 1.5,
    "PROFIT_TARGET_2": 2.5,
    "PROFIT_TARGET_3": 4.0,
    "STOP_LOSS": -2.0,
    "TRAILING_DROP": 0.8,
    "MAX_HOLD_SEC": 300,
    "STALE_SEC": 120,
    "TRADE_PCT": 0.15,
    "MIN_USD": 6,
    "MAX_OPEN": 8,
    "MIN_MOMENTUM": 0.15,
    "MIN_VOL_USD": 20_000,
    "SCAN_SEC": 1.5,
}


def scalper_worker():
    log("[SCALPER] Momentum scalper starting")
    time.sleep(8)

    scan_count = 0

    while True:
        try:
            scan_count += 1
            usdt = get_free_usdt()

            # --- EXITS ---
            with scalp_lock:
                sp_copy = dict(scalp_positions)

            for sym, pos in sp_copy.items():
                try:
                    ticker = safe_fetch_ticker(sym)
                    price = ticker["last"]
                    entry = pos["entry"]
                    pnl_pct, _, _ = calculate_pnl_after_fees(entry, price, 1.0, "long")
                    hold_sec = time.time() - pos["ts"]
                    peak = max(pos.get("peak", price), price)
                    peak_pnl = (peak - entry) / entry * 100
                    drop = peak_pnl - pnl_pct
                    tiers = pos.get("tiers_sold", 0)

                    should_exit = False
                    exit_amt = 0
                    reason = ""
                    new_tiers = tiers

                    if pnl_pct <= SCALP_CONFIG["STOP_LOSS"]:
                        should_exit, exit_amt, reason = True, pos["amount"], f"SL {pnl_pct:.1f}%"
                    elif peak_pnl >= 1.2 and drop >= SCALP_CONFIG["TRAILING_DROP"]:
                        should_exit, exit_amt, reason = True, pos["amount"], f"TRAIL {drop:.1f}%"
                    elif hold_sec >= SCALP_CONFIG["MAX_HOLD_SEC"]:
                        should_exit, exit_amt, reason = True, pos["amount"], f"TIME {pnl_pct:+.1f}%"
                    elif hold_sec >= SCALP_CONFIG["STALE_SEC"] and pnl_pct >= 0.6:
                        should_exit, exit_amt, reason = True, pos["amount"], f"STALE {pnl_pct:.1f}%"
                    elif pnl_pct >= SCALP_CONFIG["PROFIT_TARGET_1"] and tiers < 1:
                        should_exit, exit_amt, reason, new_tiers = True, pos["amount"] * 0.4, "T1 40%", 1
                    elif pnl_pct >= SCALP_CONFIG["PROFIT_TARGET_2"] and tiers < 2:
                        should_exit, exit_amt, reason, new_tiers = True, pos["amount"] * 0.4, "T2 40%", 2
                    elif pnl_pct >= SCALP_CONFIG["PROFIT_TARGET_3"]:
                        should_exit, exit_amt, reason, new_tiers = True, pos["amount"], "T3 full", 3

                    if should_exit and exit_amt > 0:
                        try:
                            exchange.create_market_sell_order(sym, exit_amt)
                            log(f"[SCALP EXIT] {sym} {reason} ({pnl_pct:+.2f}%)")
                        except Exception as e:
                            log(f"[SCALP EXIT ERROR] {sym}: {e}")

                        with scalp_lock:
                            if sym in scalp_positions:
                                scalp_positions[sym]["amount"] -= exit_amt
                                scalp_positions[sym]["peak"] = peak
                                scalp_positions[sym]["tiers_sold"] = new_tiers
                                if scalp_positions[sym]["amount"] <= 0.0001:
                                    del scalp_positions[sym]

                except Exception:
                    continue

            # --- ENTRIES ---
            with scalp_lock:
                open_count = len(scalp_positions)

            if usdt >= SCALP_CONFIG["MIN_USD"] * 1.3 and open_count < SCALP_CONFIG["MAX_OPEN"]:
                scan_list = list(set(HOT_PAIRS + CORE_PAIRS))
                random.shuffle(scan_list)
                scan_list = scan_list[:50]
                best = None
                best_score = 0

                for sym in scan_list:
                    if not sym.endswith("/USDT"):
                        continue
                    with scalp_lock:
                        if sym in scalp_positions:
                            continue
                    try:
                        ticker = safe_fetch_ticker(sym)
                        price, vol = ticker["last"], ticker.get("quoteVolume", 0) or 0
                        if price <= 0 or vol < SCALP_CONFIG["MIN_VOL_USD"]:
                            continue

                        ohlcv = safe_call(exchange.fetch_ohlcv, sym, "1m", limit=4)
                        if not ohlcv or len(ohlcv) < 3:
                            continue

                        mom_1m = (ohlcv[-1][4] - ohlcv[-1][1]) / ohlcv[-1][1] * 100
                        mom_3m = (ohlcv[-1][4] - ohlcv[-3][1]) / ohlcv[-3][1] * 100

                        if mom_1m >= SCALP_CONFIG["MIN_MOMENTUM"] or mom_3m >= 0.45:
                            score = mom_1m * 2.5 + mom_3m + (vol / 15_000_000)
                            if score > best_score:
                                best_score = score
                                best = {"sym": sym, "price": price, "mom": mom_1m, "vol": vol}
                    except Exception:
                        continue

                if best:
                    trade_size = min(usdt * SCALP_CONFIG["TRADE_PCT"],
                                    SCALP_CONFIG["MIN_USD"] * 3)
                    qty = trade_size / best["price"]
                    if qty * best["price"] >= SCALP_CONFIG["MIN_USD"]:
                        try:
                            exchange.create_market_buy_order(best["sym"], qty)
                            with scalp_lock:
                                scalp_positions[best["sym"]] = {
                                    "entry": best["price"],
                                    "amount": qty,
                                    "ts": time.time(),
                                    "peak": best["price"],
                                    "tiers_sold": 0,
                                }
                            log(f"[SCALP ENTRY] {best['sym']} +{best['mom']:.2f}% vol ${best['vol']:,.0f}")
                        except Exception as e:
                            log(f"[SCALP ENTRY ERROR] {best['sym']}: {e}")

            time.sleep(SCALP_CONFIG["SCAN_SEC"])

        except Exception as e:
            log(f"[SCALPER ERROR] {e}")
            time.sleep(3)


# =========================================================================
# WORKER: Margin Long (violent upside moves)
# =========================================================================
def margin_long_worker():
    log("[MARGIN LONG] Starting")
    time.sleep(10)

    while True:
        try:
            if not exchange:
                time.sleep(5)
                continue

            scan_list = list(HOT_PAIRS if HOT_PAIRS else CORE_PAIRS)
            random.shuffle(scan_list)
            markets = [s for s in scan_list if s.endswith("/USDT")]

            trend = get_trend_filter()

            for sym in markets:
                try:
                    ohlcv = safe_call(exchange.fetch_ohlcv, sym, "5m", limit=2)
                    if not ohlcv or len(ohlcv) < 2:
                        continue

                    prev_close = ohlcv[-2][4]
                    current = ohlcv[-1][4]
                    change = (current - prev_close) / prev_close * 100

                    ticker = safe_fetch_ticker(sym)
                    vol = ticker.get("quoteVolume", 0) or 0

                    if change > 1.5 and vol > 30_000:
                        with positions_lock:
                            if sym in positions and positions[sym].get("type") == "margin_long":
                                continue
                            margin_count = sum(1 for p in positions.values()
                                             if p.get("type") == "margin_long")
                            if margin_count >= 5:
                                continue

                        capital = get_trade_capital("margin")
                        if capital < 6:
                            continue

                        log(f"[MARGIN LONG] {sym} +{change:.1f}% vol ${vol:,.0f} -> ${capital:.2f} @ 5x")
                        margin_buy(sym, capital * 5, "margin_long")
                        time.sleep(3)

                except Exception:
                    continue

            time.sleep(20)

        except Exception as e:
            log(f"[MARGIN LONG ERROR] {e}")
            time.sleep(5)


# =========================================================================
# WORKER: Margin Short (crash hunting)
# =========================================================================
def margin_short_worker():
    log("[MARGIN SHORT] Starting")
    time.sleep(10)

    while True:
        try:
            if not exchange:
                time.sleep(5)
                continue

            scan_markets = list(set(s for s in (HOT_PAIRS or []) + CORE_PAIRS
                           if s.endswith("/USDT")))
            random.shuffle(scan_markets)
            scan_markets = scan_markets[:30]

            for sym in scan_markets:
                try:
                    ohlcv = safe_call(exchange.fetch_ohlcv, sym, "5m", limit=2)
                    if not ohlcv or len(ohlcv) < 2:
                        continue

                    prev = ohlcv[-2][4]
                    current = ohlcv[-1][4]
                    change = (current - prev) / prev * 100

                    ticker = safe_fetch_ticker(sym)
                    vol = ticker.get("quoteVolume", 0) or 0

                    if change <= -1.8 and vol > 50_000:
                        trend = get_trend_filter()
                        if trend == "up":
                            continue

                        with positions_lock:
                            if sym in positions:
                                continue
                            short_count = sum(1 for p in positions.values()
                                            if p.get("type") == "margin_short")
                            if short_count >= 5:
                                continue

                        capital = get_trade_capital("margin") * 0.5
                        if capital < 6:
                            continue

                        log(f"[MARGIN SHORT] {sym} {change:.2f}% crash -> ${capital:.2f} @ 4x")
                        margin_sell(sym, capital * 4, "margin_short")
                        time.sleep(2)

                except Exception:
                    continue

            time.sleep(18)

        except Exception as e:
            log(f"[MARGIN SHORT ERROR] {e}")
            time.sleep(5)


# =========================================================================
# WORKER: Instant Momentum
# =========================================================================
def instant_momentum_worker():
    log("[INSTANT] 2%+ momentum trader active")
    time.sleep(8)

    last_prices = {}
    trade_count = 0

    pairs = [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
        "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT",
        "MATIC/USDT", "SHIB/USDT", "UNI/USDT", "ATOM/USDT", "FIL/USDT",
        "APT/USDT", "ARB/USDT", "OP/USDT", "SUI/USDT", "NEAR/USDT",
        "INJ/USDT", "TIA/USDT", "SEI/USDT", "PEPE/USDT", "WIF/USDT",
        "FET/USDT", "RENDER/USDT", "ONDO/USDT", "JUP/USDT", "BONK/USDT",
    ]

    while True:
        try:
            if not exchange:
                time.sleep(3)
                continue

            shuffled = list(pairs)
            random.shuffle(shuffled)
            for sym in shuffled:
                try:
                    ticker = safe_fetch_ticker(sym)
                    price = ticker["last"]
                    vol = ticker.get("quoteVolume", 0) or 0

                    if price <= 0 or vol < 30_000:
                        continue

                    with positions_lock:
                        if sym in positions:
                            continue

                    if sym not in last_prices:
                        last_prices[sym] = {"price": price, "ts": time.time()}
                        continue

                    prev = last_prices[sym]
                    elapsed = time.time() - prev["ts"]
                    if elapsed < 90:
                        continue

                    change = (price - prev["price"]) / prev["price"] * 100

                    if abs(change) >= 1.5 and vol > 50_000:
                        trend = get_trend_filter()
                        direction_ok = (change > 0 and trend != "down") or \
                                       (change < 0 and trend != "up")

                        if not direction_ok:
                            last_prices[sym] = {"price": price, "ts": time.time()}
                            continue

                        if not can_trade_symbol(sym):
                            last_prices[sym] = {"price": price, "ts": time.time()}
                            continue

                        capital = get_trade_capital("spot") * 0.6
                        if capital < 6:
                            continue

                        trade_count += 1
                        if change > 0:
                            log(f"[INSTANT #{trade_count}] {sym} +{change:.1f}% LONG -> ${capital:.2f}")
                            spot_buy(sym, capital, "instant_momentum")
                        else:
                            log(f"[INSTANT #{trade_count}] {sym} {change:.1f}% SHORT -> ${capital:.2f}")
                            margin_sell(sym, capital * 3, "instant_short")

                        last_prices[sym] = {"price": price, "ts": time.time()}
                        time.sleep(2)
                    elif elapsed > 120:
                        last_prices[sym] = {"price": price, "ts": time.time()}

                except Exception:
                    continue

            time.sleep(3)

        except Exception as e:
            log(f"[INSTANT ERROR] {e}")
            time.sleep(3)


# =========================================================================
# WORKER: Solana Sniper (pump.fun + Jupiter)
# =========================================================================
def solana_sniper_worker():
    log("[SOL SNIPER] Starting...")
    time.sleep(10)

    seen_mints = set()
    session = requests.Session()

    while True:
        try:
            if not wallets or not dex:
                time.sleep(5)
                continue

            sol_wallet = wallets.solana.get() if hasattr(wallets, "solana") else None
            if not sol_wallet:
                time.sleep(5)
                continue

            sol_balance = sol_wallet.get_balance()
            if sol_balance < 0.1:
                time.sleep(10)
                continue

            sol_price = dex.get_sol_price() if dex else 230
            wallet_usd = sol_balance * sol_price
            size_usd = min(wallet_usd * 0.04, 200)  # 4% of SOL wallet, max $200
            if size_usd < 10:
                time.sleep(10)
                continue

            amount_sol = size_usd / sol_price

            # --- pump.fun ---
            try:
                r = session.get("https://pump.fun/api/recent", timeout=6)
                if r.status_code == 200:
                    launches = r.json().get("launches", [])[:20]
                    for token in launches:
                        mint = token.get("mint")
                        if not mint or mint in seen_mints:
                            continue
                        age = time.time() - token.get("created_timestamp", 0)
                        if age > 15:
                            continue

                        log(f"[PUMP] NEW {mint[-8:]} {age:.1f}s old -> {amount_sol:.3f} SOL")
                        try:
                            unsigned = dex.solana.pumpfun_build_buy(mint, amount_sol)
                            if unsigned:
                                txid = wallets.send_tx("SOL", unsigned)
                                if txid:
                                    log(f"[PUMP] BOUGHT {mint[-8:]} -> {txid}")
                        except Exception as e:
                            log(f"[PUMP] build error: {e}")
                        seen_mints.add(mint)
            except Exception:
                pass

            # --- Jupiter fresh tokens ---
            try:
                tokens = session.get("https://cache.jup.ag/tokens", timeout=5).json()
                for t in tokens[-30:]:
                    mint = t.get("address")
                    if not mint or mint in seen_mints:
                        continue
                    liq = t.get("liquidity_usd", 0)
                    if liq < 8000:
                        continue
                    try:
                        unsigned = dex.solana.build_solana_swap(mint, usd_amount=size_usd)
                        if unsigned:
                            txid = wallets.send_tx("SOL", unsigned)
                            if txid:
                                log(f"[JUP] BOUGHT {mint[-8:]} ${size_usd:.2f} -> {txid}")
                    except Exception as e:
                        log(f"[JUP] error: {e}")
                    seen_mints.add(mint)
            except Exception:
                pass

            if len(seen_mints) > 8000:
                seen_mints = set(list(seen_mints)[-5000:])

            time.sleep(1.5)

        except Exception as e:
            log(f"[SOL SNIPER ERROR] {e}")
            time.sleep(4)


# =========================================================================
# WORKER: EVM Sniper (Base, Arb, OP, AVAX, CRO)
# =========================================================================
def evm_sniper_worker():
    log("[EVM SNIPER] Starting across all EVM chains")
    time.sleep(10)

    seen_pools = set()
    chains = ["base", "arbitrum", "optimism", "avalanche", "cronos"]
    chain_key_map = {
        "base": "BASE", "arbitrum": "ARB", "optimism": "OP",
        "avalanche": "AVAX", "cronos": "CRO",
    }
    session = requests.Session()

    while True:
        try:
            if not wallets or not dex:
                time.sleep(3)
                continue

            for chain in chains:
                try:
                    url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}"
                    r = session.get(url, timeout=7)
                    if r.status_code != 200:
                        continue
                    pairs = r.json().get("pairs", [])[:40]

                    for p in pairs:
                        pool = p.get("pairAddress")
                        if not pool or pool in seen_pools:
                            continue

                        liq = p.get("liquidity", {}).get("usd", 0) or 0
                        if liq < 8000:
                            continue

                        created_ms = p.get("pairCreatedAt", 0) or 0
                        age_sec = time.time() - created_ms / 1000.0
                        if age_sec > 25:
                            continue

                        base_token = p.get("baseToken") or {}
                        token_in = base_token.get("address")
                        if not token_in:
                            continue

                        chain_key = chain_key_map.get(chain)
                        if not chain_key:
                            continue

                        evm_wallet = wallets.evm.get(chain_key)
                        if not evm_wallet or evm_wallet.get_balance() <= 0:
                            continue

                        usd_amount = min(50.0, evm_wallet.get_balance() * 3400 * 0.04)
                        if usd_amount < 15:
                            continue

                        log(f"[EVM NUCLEAR] {chain_key} {base_token.get('symbol', '???')} {age_sec:.1f}s old")

                        try:
                            unsigned = dex.evm.build_evm_swap(chain_key, token_in, usd_amount)
                            if unsigned:
                                swap = unsigned.get("swap") or unsigned
                                txid = wallets.send_tx(chain_key, unsigned)
                                if txid:
                                    log(f"[EVM] BOUGHT on {chain_key} -> {txid}")
                        except Exception as e:
                            log(f"[EVM SNIPER] build error: {e}")

                        seen_pools.add(pool)

                except Exception:
                    continue

            if len(seen_pools) > 10000:
                seen_pools = set(list(seen_pools)[-7000:])

            time.sleep(2.1)

        except Exception as e:
            log(f"[EVM SNIPER ERROR] {e}")
            time.sleep(5)


# =========================================================================
# WORKER: Orderbook Scalper
# =========================================================================
def orderbook_scalper_worker():
    log("[ORDERBOOK] Spread arb scalper active")
    time.sleep(8)

    ob_positions = {}

    while True:
        try:
            if not exchange:
                time.sleep(1)
                continue

            pairs = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
                     "BNB/USDT", "DOGE/USDT", "ADA/USDT"]

            for sym in pairs:
                try:
                    # Check exits
                    if sym in ob_positions:
                        pos = ob_positions[sym]
                        if time.time() - pos["ts"] < 8:
                            continue
                        ticker = safe_fetch_ticker(sym)
                        price = ticker["last"]
                        pnl_pct, _, _ = calculate_pnl_after_fees(pos["entry"], price, 1.0, "long")
                        age = time.time() - pos["ts"]

                        if pnl_pct >= 0.4 or pnl_pct <= -0.4 or age > 90:
                            try:
                                exchange.create_market_sell_order(sym, pos["amount"])
                                log(f"[OB EXIT] {sym} {pnl_pct:+.2f}% ({age:.0f}s)")
                                del ob_positions[sym]
                            except Exception:
                                pass
                        continue

                    # Entry
                    ob = safe_call(exchange.fetch_order_book, sym, limit=10)
                    if not ob or not ob.get("bids") or not ob.get("asks"):
                        continue

                    best_bid = float(ob["bids"][0][0])
                    best_ask = float(ob["asks"][0][0])
                    bid_size = float(ob["bids"][0][1])
                    ask_size = float(ob["asks"][0][1])

                    spread = (best_ask - best_bid) / best_bid * 100
                    if not (MIN_PROFITABLE_SPREAD < spread < 1.0):
                        continue

                    if bid_size * best_bid < 5000 or ask_size * best_ask < 5000:
                        continue

                    if len(ob_positions) >= 2:
                        continue

                    usdt = get_free_usdt()
                    if usdt < 20:
                        continue

                    trade_size = min(usdt * 0.12, 45)
                    qty = trade_size / best_ask
                    if qty * best_ask < 15:
                        continue

                    order = safe_call(exchange.create_market_buy_order, sym, qty)
                    if order:
                        ob_positions[sym] = {
                            "entry": best_ask, "amount": qty, "ts": time.time(),
                        }
                        log(f"[OB ARB] {sym} spread {spread:.3f}% -> ${trade_size:.0f}")

                    time.sleep(0.8)

                except Exception:
                    continue

            time.sleep(1.2)

        except Exception as e:
            log(f"[ORDERBOOK ERROR] {e}")
            time.sleep(2)


# =========================================================================
# WORKER: Steady Climber
# =========================================================================
def steady_climber_worker():
    log("[STEADY CLIMBER] Starting")
    time.sleep(10)

    price_history = {}

    while True:
        try:
            if not exchange:
                time.sleep(5)
                continue

            scan = list(set(s for s in (HOT_PAIRS or []) + CORE_PAIRS
                    if s.endswith("/USDT")))
            random.shuffle(scan)
            scan = scan[:30]
            now = time.time()

            for sym in scan:
                try:
                    ticker = safe_fetch_ticker(sym)
                    price = ticker["last"]
                    vol = ticker.get("quoteVolume", 0) or 0
                    if price <= 0 or vol < 75_000:
                        continue

                    if sym not in price_history:
                        price_history[sym] = {"price": price, "ts": now}
                        continue

                    hist = price_history[sym]
                    hours = (now - hist["ts"]) / 3600

                    if hours < 2 or hours > 6:
                        price_history[sym] = {"price": price, "ts": now}
                        continue

                    change = (price - hist["price"]) / hist["price"] * 100

                    if change >= 3.5 and get_trend_filter() != "down":
                        if can_trade_symbol(sym):
                            capital = get_trade_capital("spot") * 0.45
                            if capital > 6:
                                log(f"[STEADY] {sym} +{change:.2f}% in {hours:.1f}h -> ${capital:.2f}")
                                spot_buy(sym, capital, "steady_climber")
                                price_history[sym]["ts"] = now
                                time.sleep(2)

                except Exception:
                    continue

            time.sleep(30)

        except Exception as e:
            log(f"[STEADY ERROR] {e}")
            time.sleep(5)


# =========================================================================
# WORKER: Aggressive Profit Taker
# =========================================================================
def aggressive_profit_taker():
    log("[PROFIT TAKER] Starting")
    time.sleep(10)

    while True:
        try:
            if not exchange:
                time.sleep(5)
                continue

            now = time.time()

            with positions_lock:
                pos_copy = dict(positions)

            for sym, pos in pos_copy.items():
                try:
                    ticker = safe_fetch_ticker(sym)
                    price = ticker["last"]
                    if price <= 0:
                        continue

                    entry = pos.get("entry", price)
                    age_min = (now - pos.get("time", now)) / 60
                    pnl_pct, _, _ = calculate_pnl_after_fees(entry, price, 1.0, "long")

                    # AI: RSI > 75 triggers partial profit taking
                    if AI and pnl_pct > 0.5:
                        try:
                            if AI.should_take_partial_profit(exchange, sym):
                                log(f"[AI RSI EXIT] {sym} RSI>75 + {pnl_pct:+.1f}% -> sell 50%")
                                spot_sell(sym, reason="ai_rsi_overbought", fraction=0.5)
                                time.sleep(0.7)
                                continue
                        except Exception:
                            pass

                    # Age-based thresholds
                    if age_min < 15:
                        threshold = 4.0
                    elif age_min < 45:
                        threshold = 3.0
                    elif age_min < 90:
                        threshold = 2.0
                    elif age_min < 180:
                        threshold = 1.5
                    else:
                        threshold = 0.75

                    if pnl_pct >= threshold:
                        if age_min > 180:
                            fraction = 0.8
                        elif age_min > 90:
                            fraction = 0.6
                        else:
                            fraction = 0.4

                        log(f"[PROFIT LOCK] {sym} +{pnl_pct:.1f}% after {age_min:.0f}min -> sell {fraction*100:.0f}%")
                        spot_sell(sym, reason="profit_take", fraction=fraction)
                        time.sleep(0.7)

                except Exception:
                    continue

            time.sleep(45)

        except Exception as e:
            log(f"[PROFIT TAKER ERROR] {e}")
            time.sleep(15)


# =========================================================================
# WORKER: Stale Position Cleaner
# =========================================================================
def stale_position_cleaner():
    log("[STALE CLEANER] Starting")
    time.sleep(15)

    while True:
        try:
            if not exchange:
                time.sleep(10)
                continue

            now = time.time()
            max_age = STALE_CONFIG["max_age_hours"] * 3600

            with positions_lock:
                pos_copy = dict(positions)

            for sym, pos in pos_copy.items():
                try:
                    age = now - pos.get("time", now)
                    if age < max_age:
                        continue

                    ticker = safe_fetch_ticker(sym)
                    price = ticker["last"]
                    if price <= 0:
                        continue

                    entry = pos.get("entry", price)
                    pnl_pct, _, _ = calculate_pnl_after_fees(entry, price, 1.0, "long")

                    # Keep profitable positions
                    if pnl_pct >= 2.0:
                        continue

                    hours = age / 3600
                    log(f"[STALE] {sym} {pnl_pct:+.1f}% after {hours:.1f}h -> selling")
                    spot_sell(sym, reason="stop_loss")
                    time.sleep(1)

                except Exception:
                    continue

            time.sleep(STALE_CONFIG["check_interval"])

        except Exception as e:
            log(f"[STALE ERROR] {e}")
            time.sleep(30)


# =========================================================================
# WORKER: Portfolio Manager
# =========================================================================
def portfolio_manager_worker():
    log("[PORTFOLIO] Starting")
    time.sleep(10)

    MIN_USDT_RESERVE = 25
    MAX_PORTFOLIO = 10
    REBALANCE_INTERVAL = 300

    while True:
        try:
            if not exchange:
                time.sleep(5)
                continue

            bal = safe_call(exchange.fetch_balance)
            if not bal:
                time.sleep(30)
                continue

            holdings = []
            usdt_bal = 0

            for coin, amount in bal.get("total", {}).items():
                if float(amount) <= 0:
                    continue
                if coin in ["USDT", "USDC", "BUSD"]:
                    usdt_bal += float(amount)
                    continue
                try:
                    ticker = exchange.fetch_ticker(f"{coin}/USDT")
                    price = ticker.get("last", 0)
                    usd_val = float(amount) * price
                    if usd_val < 2:
                        continue
                    change_24h = ticker.get("percentage", 0) or 0
                    holdings.append({
                        "coin": coin, "symbol": f"{coin}/USDT",
                        "amount": float(amount), "price": price,
                        "usd": usd_val, "change_24h": change_24h,
                    })
                except Exception:
                    continue

            # Sell weak performers
            for h in holdings:
                if h["change_24h"] < -8 and h["usd"] > 5:
                    try:
                        log(f"[PORTFOLIO] Selling weak {h['coin']} ({h['change_24h']:+.1f}%)")
                        exchange.create_market_sell_order(h["symbol"], h["amount"])
                        time.sleep(1)
                    except Exception:
                        pass

            # Buy opportunities if we have USDT
            if usdt_bal >= MIN_USDT_RESERVE and len(holdings) < MAX_PORTFOLIO:
                scan = HOT_PAIRS if HOT_PAIRS else CORE_PAIRS
                current_coins = {h["coin"] for h in holdings}

                for sym in scan[:30]:
                    if not sym.endswith("/USDT"):
                        continue
                    coin = sym.split("/")[0]
                    if coin in current_coins:
                        continue
                    try:
                        ticker = exchange.fetch_ticker(sym)
                        change = ticker.get("percentage", 0) or 0
                        vol = ticker.get("quoteVolume", 0) or 0
                        if change > 3 and vol > 50_000:
                            buy_amt = min(usdt_bal * 0.15, 50, usdt_bal - MIN_USDT_RESERVE)
                            if buy_amt >= 5:
                                qty = buy_amt / ticker["last"]
                                log(f"[PORTFOLIO] Buying {coin} +{change:.1f}% -> ${buy_amt:.2f}")
                                exchange.create_market_buy_order(sym, qty)
                                break
                    except Exception:
                        continue

            time.sleep(REBALANCE_INTERVAL)

        except Exception as e:
            log(f"[PORTFOLIO ERROR] {e}")
            time.sleep(30)


# =========================================================================
# WORKER: Position Age Logger
# =========================================================================
def log_position_ages():
    while True:
        try:
            time.sleep(1800)  # 30 min

            with positions_lock:
                pos_copy = dict(positions)
            with futures_lock:
                fut_copy = dict(futures_positions)

            if not pos_copy and not fut_copy:
                continue

            now = time.time()
            report = []
            for sym, pos in pos_copy.items():
                hours = (now - pos.get("time", now)) / 3600
                ticker = safe_fetch_ticker(sym)
                price = ticker.get("last", 0)
                if price > 0:
                    entry = pos.get("entry", price)
                    pnl = (price - entry) / entry * 100
                    report.append(f"{sym.split('/')[0]}:{hours:.1f}h {pnl:+.1f}%")

            if report:
                log(f"[POSITIONS] {len(pos_copy)} open: {', '.join(report[:8])}")

        except Exception:
            time.sleep(1800)


# =========================================================================
# WORKER: Perps (imported from empire_perps)
# =========================================================================
try:
    from empire_perps import perps_trading_worker, perps_monitor
except ImportError:
    def perps_trading_worker():
        log("[PERPS] empire_perps not found — skipping")

    def perps_monitor():
        pass


# =========================================================================
# WORKER: Profit Harvester (24h cycle)
# =========================================================================
def profit_harvester_worker():
    log("[HARVESTER] Starting — 70% profit harvest every 24h")
    time.sleep(30)

    last_balance = get_total_balance_usd()

    while True:
        try:
            time.sleep(86400)  # 24 hours

            if not exchange:
                continue

            current = get_total_balance_usd()
            profit = current - last_balance

            if profit <= 0:
                log(f"[HARVESTER] No profit (${profit:.2f})")
                last_balance = current
                continue

            harvest = profit * 0.70
            log(f"[HARVESTER] Profit ${profit:.2f} -> harvesting 70%: ${harvest:.2f}")

            harvested = 0
            bal = safe_call(exchange.fetch_balance)
            if bal:
                for coin, amount in bal.get("free", {}).items():
                    if harvested >= harvest:
                        break
                    if float(amount) <= 0 or coin in ["USDT", "USDC"]:
                        continue
                    try:
                        sym = f"{coin}/USDT"
                        ticker = exchange.fetch_ticker(sym)
                        price = ticker["last"]
                        to_sell = min(harvest - harvested, float(amount) * price * 0.3)
                        if to_sell < 5:
                            continue
                        qty = to_sell / price
                        exchange.create_market_sell_order(sym, qty)
                        harvested += to_sell
                        time.sleep(0.5)
                    except Exception:
                        continue

            log(f"[HARVESTER] Done — harvested ${harvested:.2f}")
            last_balance = current

        except Exception as e:
            log(f"[HARVESTER ERROR] {e}")
            time.sleep(60)


# =========================================================================
# EMERGENCY LIQUIDATION
# =========================================================================
def emergency_liquidate_all():
    log("[EMERGENCY] Liquidating ALL positions!")
    if not exchange:
        return
    try:
        bal = exchange.fetch_balance()
        for coin, amount in bal.get("total", {}).items():
            if float(amount) <= 0 or coin in ["USDT", "USDC", "BUSD"]:
                continue
            try:
                exchange.create_market_sell_order(f"{coin}/USDT", float(amount))
                log(f"[EMERGENCY] Sold {amount} {coin}")
                time.sleep(0.5)
            except Exception:
                pass

        time.sleep(2)
        final = exchange.fetch_balance()
        usdt = final.get("total", {}).get("USDT", 0)
        log(f"[EMERGENCY] Done. USDT: ${usdt:.2f}")
    except Exception as e:
        log(f"[EMERGENCY] Failed: {e}")


# =========================================================================
# SOL CONSOLIDATION — Move scattered SOL to primary wallet
# =========================================================================
def consolidate_sol_worker():
    """Periodically consolidate SOL from all wallets into the primary wallet."""
    log("[CONSOLIDATE] SOL consolidation worker starting")
    time.sleep(30)

    while True:
        try:
            if not wallets or not wallets.solana:
                time.sleep(300)
                continue

            transferred = wallets.solana.consolidate_sol()
            if transferred > 0:
                sol_amount = transferred / 1_000_000_000
                log(f"[CONSOLIDATE] Moved {sol_amount:.5f} SOL to primary wallet")

            time.sleep(600)  # Check every 10 minutes
        except Exception as e:
            log(f"[CONSOLIDATE ERROR] {e}")
            time.sleep(300)


# =========================================================================
# DRIP SELL — Liquidate low-liquidity SPL tokens in tiny batches
# =========================================================================
def drip_sell_worker():
    """
    Scans all Solana wallets for SPL token holdings and drip-sells them
    to SOL via Jupiter in small batches to avoid slippage.
    Runs continuously — checks every 2 minutes for tokens to sell.
    """
    log("[DRIP SELL] SPL token liquidator starting...")
    time.sleep(15)  # Let everything else initialize first

    SOL_MINT = "So11111111111111111111111111111111111111112"
    TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

    # Configurable per-batch settings
    MAX_BATCH_USD = 1.50          # Max ~$1.50 per swap to keep slippage low
    DELAY_BETWEEN_SWAPS = 15      # Seconds between swaps (let pool recover)
    MIN_TOKEN_VALUE_USD = 0.10    # Skip tokens worth less than 10 cents total
    MAX_SLIPPAGE_BPS = 1500       # 15% max slippage — skip if worse

    while True:
        try:
            if not wallets or not dex or not dex.solana:
                time.sleep(60)
                continue

            sol_price = dex.get_sol_price()
            total_sold_usd = 0

            for sol_wallet in wallets.solana.wallets:
                if not sol_wallet.pubkey:
                    continue

                pubkey_str = str(sol_wallet.pubkey)

                # Query all SPL token accounts for this wallet
                try:
                    res = sol_wallet.client.post(sol_wallet.rpc_url, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getTokenAccountsByOwner",
                        "params": [
                            pubkey_str,
                            {"programId": TOKEN_PROGRAM},
                            {"encoding": "jsonParsed"}
                        ]
                    }, timeout=15).json()

                    accounts = res.get("result", {}).get("value", [])
                except Exception as e:
                    log(f"[DRIP SELL] RPC error scanning {pubkey_str[:8]}: {e}")
                    continue

                for acct in accounts:
                    try:
                        info = acct["account"]["data"]["parsed"]["info"]
                        mint = info["mint"]
                        raw_amount = int(info["tokenAmount"]["amount"])
                        decimals = info["tokenAmount"]["decimals"]
                        ui_amount = raw_amount / (10 ** decimals) if decimals > 0 else raw_amount

                        # Skip native SOL wrapper and empty balances
                        if mint == SOL_MINT or raw_amount <= 0:
                            continue

                        # Get a quote for the FULL amount to estimate USD value
                        try:
                            full_quote = dex.solana.get_jupiter_quote(
                                input_mint=mint,
                                output_mint=SOL_MINT,
                                amount=raw_amount,
                            )
                            if "error" in full_quote or "outAmount" not in full_quote:
                                continue

                            out_lamports = int(full_quote["outAmount"])
                            total_value_usd = (out_lamports / 1e9) * sol_price

                            if total_value_usd < MIN_TOKEN_VALUE_USD:
                                continue

                        except Exception:
                            continue

                        # Calculate batch size in raw token units
                        # If $45 total and we want $1.50 per batch, that's ~3.3% per batch
                        if total_value_usd <= MAX_BATCH_USD:
                            batch_amount = raw_amount  # Sell it all in one go
                        else:
                            batch_fraction = MAX_BATCH_USD / total_value_usd
                            batch_amount = max(1, int(raw_amount * batch_fraction))

                        log(f"[DRIP SELL] Found {ui_amount:.2f} tokens of {mint[:8]}... "
                            f"(~${total_value_usd:.2f}) in wallet {pubkey_str[:8]}... "
                            f"— selling in ${MAX_BATCH_USD:.2f} batches")

                        # Drip sell loop — use THIS wallet's pubkey for Jupiter
                        remaining = raw_amount
                        batch_num = 0
                        wallet_pubkey = pubkey_str
                        session = dex.solana.session

                        while remaining > 0:
                            sell_amount = min(batch_amount, remaining)
                            batch_num += 1

                            try:
                                # Get quote for this batch
                                quote = dex.solana.get_jupiter_quote(
                                    input_mint=mint,
                                    output_mint=SOL_MINT,
                                    amount=sell_amount,
                                )

                                if "error" in quote or "outAmount" not in quote:
                                    log(f"[DRIP SELL] No route for batch #{batch_num} — skipping")
                                    break

                                # Check slippage
                                price_impact = float(quote.get("priceImpactPct", "0") or "0")
                                if abs(price_impact) * 100 > MAX_SLIPPAGE_BPS / 100:
                                    log(f"[DRIP SELL] Slippage {price_impact*100:.1f}% too high "
                                        f"on batch #{batch_num} — waiting longer")
                                    time.sleep(DELAY_BETWEEN_SWAPS * 3)
                                    sell_amount = max(1, sell_amount // 2)
                                    continue

                                out_sol = int(quote["outAmount"]) / 1e9
                                batch_usd = out_sol * sol_price

                                # Build swap TX using THIS wallet's pubkey
                                swap_data = {
                                    "quoteResponse": quote,
                                    "userPublicKey": wallet_pubkey,
                                    "wrapAndUnwrapSol": True,
                                }
                                r = session.post(
                                    dex.solana._jupiter_swap_url,
                                    json=swap_data, timeout=10
                                )
                                js = r.json()
                                tx_b64 = js.get("swapTransaction")

                                if not tx_b64:
                                    log(f"[DRIP SELL] Swap TX build failed batch #{batch_num}: {js}")
                                    break

                                # SIGN and send with this wallet's keypair
                                txid = sol_wallet.sign_and_send_jupiter_tx(tx_b64)

                                if txid and isinstance(txid, str):
                                    human_sold = sell_amount / (10 ** decimals)
                                    remaining -= sell_amount
                                    total_sold_usd += batch_usd
                                    pct_done = ((raw_amount - remaining) / raw_amount) * 100
                                    log(f"[DRIP SELL] Batch #{batch_num}: sold {human_sold:.2f} "
                                        f"-> {out_sol:.4f} SOL (~${batch_usd:.2f}) "
                                        f"| {pct_done:.0f}% done | tx: {txid[:16]}...")
                                else:
                                    error_msg = ""
                                    if isinstance(txid, dict):
                                        error_msg = txid.get("error", {}).get("message", str(txid))
                                    else:
                                        error_msg = str(txid)
                                    log(f"[DRIP SELL] TX failed batch #{batch_num}: {error_msg}")
                                    time.sleep(DELAY_BETWEEN_SWAPS)
                                    continue

                            except Exception as e:
                                log(f"[DRIP SELL] Batch #{batch_num} error: {e}")
                                time.sleep(DELAY_BETWEEN_SWAPS)
                                continue

                            # Wait for pool to recover before next batch
                            time.sleep(DELAY_BETWEEN_SWAPS)

                        if batch_num > 0:
                            log(f"[DRIP SELL] Finished {mint[:8]}... — "
                                f"{batch_num} batches, ~${total_sold_usd:.2f} recovered")

                    except Exception as e:
                        continue

            # Wait before scanning again
            if total_sold_usd > 0:
                log(f"[DRIP SELL] Cycle complete — ${total_sold_usd:.2f} total recovered to SOL")
            time.sleep(120)  # Re-scan every 2 minutes

        except Exception as e:
            log(f"[DRIP SELL ERROR] {e}")
            time.sleep(60)


# =========================================================================
# SYNC POSITIONS WITH EXCHANGE
# =========================================================================
def sync_positions_with_exchange():
    """Sync position dict with actual exchange holdings on startup."""
    if not exchange:
        return
    try:
        bal = safe_call(exchange.fetch_balance)
        if not bal:
            return
        for coin, amount in bal.get("total", {}).items():
            if float(amount) <= 0 or coin in ["USDT", "USDC", "BUSD"]:
                continue
            sym = f"{coin}/USDT"
            try:
                ticker = exchange.fetch_ticker(sym)
                price = ticker["last"]
                usd_val = float(amount) * price
                if usd_val >= 5:
                    with positions_lock:
                        if sym not in positions:
                            positions[sym] = {
                                "entry": price,
                                "qty": float(amount),
                                "usd": usd_val,
                                "time": time.time(),
                                "strategy": "synced",
                                "type": "spot",
                                "high": price,
                            }
            except Exception:
                continue
        log(f"[SYNC] Synced {len(positions)} positions from exchange")
    except Exception as e:
        log(f"[SYNC ERROR] {e}")


# =========================================================================
# AI-POWERED WORKERS — Mean Reversion, EMA Crossover, Momentum, Grid
# =========================================================================

def mean_reversion_worker():
    """Buy when RSI < 25 + price below lower Bollinger Band, sell when RSI > 70."""
    log("[MEAN REVERT] AI mean reversion worker starting")
    time.sleep(15)
    if not AI:
        log("[MEAN REVERT] AI module not available — exiting")
        return
    while True:
        try:
            if not exchange:
                time.sleep(5)
                continue
            # Check exits first
            with positions_lock:
                pos_copy = {s: p for s, p in positions.items()
                            if p.get("strategy") == "mean_reversion"}
            for sym, pos in pos_copy.items():
                try:
                    ai = AI.get_ai_score(exchange, sym)
                    if ai["rsi_1h"] > 70:
                        log(f"[MEAN REVERT EXIT] {sym} RSI={ai['rsi_1h']:.0f} -> selling")
                        spot_sell(sym, reason="mean_revert_rsi_exit")
                except Exception:
                    continue
            # Scan for entries
            scan = list(HOT_PAIRS if HOT_PAIRS else CORE_PAIRS)
            random.shuffle(scan)
            for sym in scan[:30]:
                if not sym.endswith("/USDT"):
                    continue
                if not can_trade_symbol(sym):
                    continue
                try:
                    ai = AI.get_ai_score(exchange, sym)
                    if ai["rsi_1h"] < 25 and ai["bb_pct_b"] < 0.05:
                        vol = get_24h_volume(sym)
                        if vol < 50_000:
                            continue
                        capital = get_trade_capital("mean_reversion", sym)
                        if capital < MIN_TRADE_SIZE_USD:
                            continue
                        log(f"[MEAN REVERT] {sym} RSI={ai['rsi_1h']:.0f} BB%={ai['bb_pct_b']:.2f} score={ai['score']:.0f} -> ${capital:.2f}")
                        spot_buy(sym, capital, "mean_reversion")
                        time.sleep(3)
                except Exception:
                    continue
            time.sleep(30)
        except Exception as e:
            log(f"[MEAN REVERT ERROR] {e}")
            time.sleep(10)


def ema_crossover_worker():
    """Enter on EMA12/EMA26 golden cross, exit on death cross."""
    log("[EMA CROSS] AI EMA crossover worker starting")
    time.sleep(15)
    if not AI:
        log("[EMA CROSS] AI module not available — exiting")
        return
    prev_cross = {}  # sym -> "above" | "below"
    while True:
        try:
            if not exchange:
                time.sleep(5)
                continue
            scan = list(set(s for s in (HOT_PAIRS or []) + CORE_PAIRS if s.endswith("/USDT")))
            random.shuffle(scan)
            for sym in scan[:25]:
                try:
                    candles = AI.fetch_ohlcv_cached(exchange, sym, "1h", 200)
                    if not candles or len(candles) < 30:
                        continue
                    closes = [c[4] for c in candles]
                    ema12 = AI.compute_ema(closes, 12)
                    ema26 = AI.compute_ema(closes, 26)
                    current_rel = "above" if ema12[-1] > ema26[-1] else "below"
                    prev_rel = prev_cross.get(sym)
                    prev_cross[sym] = current_rel
                    if prev_rel is None:
                        continue
                    # Golden cross: EMA12 crosses above EMA26
                    if prev_rel == "below" and current_rel == "above":
                        if not can_trade_symbol(sym):
                            continue
                        ai = AI.get_ai_score(exchange, sym)
                        if ai["score"] < 45:
                            continue
                        vol = get_24h_volume(sym)
                        if vol < 50_000:
                            continue
                        capital = get_trade_capital("ema_crossover", sym)
                        if capital < MIN_TRADE_SIZE_USD:
                            continue
                        log(f"[EMA CROSS] GOLDEN {sym} score={ai['score']:.0f} -> ${capital:.2f}")
                        spot_buy(sym, capital, "ema_crossover")
                        time.sleep(2)
                    # Death cross: exit existing ema_crossover positions
                    elif prev_rel == "above" and current_rel == "below":
                        with positions_lock:
                            pos = positions.get(sym)
                            if pos and pos.get("strategy") == "ema_crossover":
                                log(f"[EMA CROSS] DEATH {sym} -> selling")
                                spot_sell(sym, reason="death_cross")
                except Exception:
                    continue
            time.sleep(60)
        except Exception as e:
            log(f"[EMA CROSS ERROR] {e}")
            time.sleep(10)


def ai_momentum_worker():
    """Only enter when composite AI score > 70, aggressive sizing."""
    log("[AI MOMENTUM] AI high-conviction momentum worker starting")
    time.sleep(15)
    if not AI:
        log("[AI MOMENTUM] AI module not available — exiting")
        return
    while True:
        try:
            if not exchange:
                time.sleep(5)
                continue
            # Prioritize trending coins
            trending_syms = AI.get_trending_symbols()
            scan = list(set(trending_syms + (HOT_PAIRS if HOT_PAIRS else CORE_PAIRS)))
            random.shuffle(scan)
            for sym in scan[:30]:
                if not sym.endswith("/USDT"):
                    continue
                if not can_trade_symbol(sym):
                    continue
                try:
                    ai = AI.get_ai_score(exchange, sym)
                    if ai["score"] >= 70 and ai["ema_trend"] == "bullish":
                        vol = get_24h_volume(sym)
                        if vol < 75_000:
                            continue
                        capital = get_trade_capital("ai_momentum", sym) * 1.5
                        capital = min(capital, MAX_TRADE_SIZE_USD)
                        if capital < MIN_TRADE_SIZE_USD:
                            continue
                        log(f"[AI MOM] {sym} SCORE={ai['score']:.0f} RSI={ai['rsi_1h']:.0f} trend={ai['ema_trend']} -> ${capital:.2f}")
                        spot_buy(sym, capital, "ai_momentum")
                        time.sleep(3)
                except Exception:
                    continue
            time.sleep(45)
        except Exception as e:
            log(f"[AI MOMENTUM ERROR] {e}")
            time.sleep(10)


def grid_trading_worker():
    """Place buy/sell grid around current price in sideways markets (ATR-based spacing)."""
    log("[GRID] AI grid trading worker starting")
    time.sleep(15)
    if not AI:
        log("[GRID] AI module not available — exiting")
        return
    active_grids = {}  # sym -> {center, buy_levels, sell_levels, filled_buys, filled_sells, ts}
    MAX_GRIDS = 3
    while True:
        try:
            if not exchange:
                time.sleep(5)
                continue
            # Manage existing grids
            for sym, grid in list(active_grids.items()):
                try:
                    ticker = safe_fetch_ticker(sym)
                    if not ticker or not ticker.get("last"):
                        continue
                    price = ticker["last"]
                    ai = AI.get_ai_score(exchange, sym)
                    # Market is trending now — close grid
                    if ai["adx"] > 30:
                        log(f"[GRID] {sym} ADX={ai['adx']:.0f} trending -> closing grid")
                        with positions_lock:
                            if sym in positions:
                                spot_sell(sym, reason="grid_trend_exit")
                        del active_grids[sym]
                        continue
                    # Buy at lower grid levels
                    for level in grid["buy_levels"]:
                        if price <= level and level not in grid["filled_buys"]:
                            capital = get_trade_capital("grid", sym) * 0.3
                            if capital >= MIN_TRADE_SIZE_USD and can_trade_symbol(sym):
                                log(f"[GRID BUY] {sym} @ level ${level:.6f}")
                                if spot_buy(sym, capital, "grid"):
                                    grid["filled_buys"].add(level)
                    # Sell at upper grid levels
                    for level in grid["sell_levels"]:
                        if price >= level and level not in grid["filled_sells"]:
                            with positions_lock:
                                if sym in positions:
                                    log(f"[GRID SELL] {sym} @ level ${level:.6f}")
                                    spot_sell(sym, reason="grid_profit", fraction=0.3)
                                    grid["filled_sells"].add(level)
                    # Grid expired after 4 hours
                    if time.time() - grid["ts"] > 14400:
                        log(f"[GRID] {sym} expired -> closing")
                        with positions_lock:
                            if sym in positions:
                                spot_sell(sym, reason="grid_expire")
                        del active_grids[sym]
                except Exception:
                    continue
            # Set up new grids in sideways markets
            if len(active_grids) < MAX_GRIDS:
                scan = list(CORE_PAIRS)
                random.shuffle(scan)
                for sym in scan[:15]:
                    if sym in active_grids:
                        continue
                    try:
                        ai = AI.get_ai_score(exchange, sym)
                        if ai["adx"] > 20:
                            continue
                        vol = get_24h_volume(sym)
                        if vol < 100_000:
                            continue
                        ticker = safe_fetch_ticker(sym)
                        price = ticker["last"]
                        candles = AI.fetch_ohlcv_cached(exchange, sym, "1h", 200)
                        atr = AI.compute_atr(candles, 14)
                        if atr <= 0:
                            continue
                        spacing = atr * 0.5
                        num_levels = 3
                        buy_levels = [price - spacing * (i + 1) for i in range(num_levels)]
                        sell_levels = [price + spacing * (i + 1) for i in range(num_levels)]
                        active_grids[sym] = {
                            "center": price,
                            "buy_levels": buy_levels,
                            "sell_levels": sell_levels,
                            "filled_buys": set(),
                            "filled_sells": set(),
                            "ts": time.time(),
                        }
                        log(f"[GRID] New grid: {sym} center=${price:.6f} spacing=${spacing:.6f} ADX={ai['adx']:.0f}")
                        break
                    except Exception:
                        continue
            time.sleep(30)
        except Exception as e:
            log(f"[GRID ERROR] {e}")
            time.sleep(10)


# =========================================================================
# ENGINE BOOT
# =========================================================================
def start_empire():
    """Launch all worker threads with auto-restart wrapping."""
    _init_db()
    _migrate_db()

    workers = [
        # Core scanners
        hot_token_scanner_worker,
        breakout_worker,
        new_listing_sniper_worker,
        volume_surge_worker,

        # On-chain snipers
        solana_sniper_worker,
        evm_sniper_worker,

        # Scalpers
        scalper_worker,
        orderbook_scalper_worker,

        # Margin engines
        margin_short_worker,
        margin_long_worker,
        steady_climber_worker,
        instant_momentum_worker,

        # Risk management
        stop_loss_worker,
        stale_position_cleaner,
        aggressive_profit_taker,

        # Portfolio
        portfolio_manager_worker,
        profit_harvester_worker,

        # Monitoring
        log_position_ages,

        # SPL token liquidator + SOL consolidation
        drip_sell_worker,
        consolidate_sol_worker,

        # AI-powered strategies
        mean_reversion_worker,
        ema_crossover_worker,
        ai_momentum_worker,
        grid_trading_worker,

        # Perps
        perps_trading_worker,
        perps_monitor,
    ]

    for w in workers:
        try:
            wrapped = resilient_thread(w, name=w.__name__)
            t = threading.Thread(target=wrapped, daemon=True)
            t.start()
            log(f"[CORE] Started: {w.__name__} (auto-restart enabled)")
        except Exception as e:
            log(f"[CORE FAIL] {w.__name__}: {e}")

    log(f"[CORE] Empire running — {len(workers)} workers launched")
