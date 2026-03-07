# ================================================================
# EMPIRE AI — Signal Engine
# ================================================================
# Technical indicators computed from OHLCV data (pure math, no deps).
# Composite scoring: RSI, MACD, Bollinger, EMA, ADX, Fear & Greed.
# Thread-safe with per-resource caching.
# ================================================================

import time
import math
import threading
import requests

_ai_lock = threading.Lock()
_session = requests.Session()
_session.headers.update({"User-Agent": "EmpireBot/1.0"})


# ================================================================
# OHLCV CACHE
# ================================================================
_ohlcv_cache = {}  # key: (symbol, timeframe) -> {"candles": [...], "ts": float}
_OHLCV_TTL = 60


def fetch_ohlcv_cached(exchange, symbol: str, timeframe: str = "1h", limit: int = 200) -> list:
    """Fetch OHLCV candles via CCXT with caching. Returns list of [ts, o, h, l, c, vol]."""
    key = (symbol, timeframe)
    now = time.time()
    cached = _ohlcv_cache.get(key)
    if cached and now - cached["ts"] < _OHLCV_TTL:
        return cached["candles"]
    try:
        candles = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        if candles and len(candles) >= 2:
            _ohlcv_cache[key] = {"candles": candles, "ts": now}
            return candles
    except Exception:
        pass
    return cached["candles"] if cached else []


# ================================================================
# TECHNICAL INDICATORS — Pure Math
# ================================================================

def compute_rsi(closes: list, period: int = 14) -> float:
    """Relative Strength Index (Wilder's smoothing). Returns 0-100."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas[:period]]
    losses = [-d if d < 0 else 0 for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_ema(values: list, period: int) -> list:
    """Exponential Moving Average. Returns list same length as input."""
    if len(values) < period:
        return values[:]
    k = 2.0 / (period + 1)
    ema = [0.0] * len(values)
    ema[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema


def compute_macd(closes: list) -> dict:
    """MACD (12, 26, 9). Returns {macd, signal, histogram}."""
    if len(closes) < 35:
        return {"macd": 0, "signal": 0, "histogram": 0}
    ema12 = compute_ema(closes, 12)
    ema26 = compute_ema(closes, 26)
    macd_line = [ema12[i] - ema26[i] for i in range(len(closes))]
    signal_line = compute_ema(macd_line[25:], 9)
    macd_val = macd_line[-1]
    signal_val = signal_line[-1] if signal_line else 0
    return {"macd": macd_val, "signal": signal_val, "histogram": macd_val - signal_val}


def compute_bollinger(closes: list, period: int = 20, std_dev: float = 2.0) -> dict:
    """Bollinger Bands. Returns {upper, middle, lower, pct_b}."""
    if len(closes) < period:
        return {"upper": 0, "middle": 0, "lower": 0, "pct_b": 0.5}
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = math.sqrt(variance)
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    price = closes[-1]
    band_width = upper - lower
    pct_b = (price - lower) / band_width if band_width > 0 else 0.5
    return {"upper": upper, "middle": middle, "lower": lower, "pct_b": pct_b}


def compute_atr(candles: list, period: int = 14) -> float:
    """Average True Range from OHLCV candles."""
    if len(candles) < period + 1:
        return 0.0
    true_ranges = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i][2], candles[i][3], candles[i - 1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    if len(true_ranges) < period:
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def compute_adx(candles: list, period: int = 14) -> float:
    """Average Directional Index. Returns 0-100. >25 = trending, <20 = sideways."""
    if len(candles) < period * 2 + 1:
        return 0.0
    plus_dm_list, minus_dm_list, tr_list = [], [], []
    for i in range(1, len(candles)):
        high, low = candles[i][2], candles[i][3]
        prev_high, prev_low, prev_close = candles[i - 1][2], candles[i - 1][3], candles[i - 1][4]
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm = max(up_move, 0) if up_move > down_move else 0
        minus_dm = max(down_move, 0) if down_move > up_move else 0
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
        tr_list.append(tr)
    smooth_plus = sum(plus_dm_list[:period])
    smooth_minus = sum(minus_dm_list[:period])
    smooth_tr = sum(tr_list[:period])
    dx_list = []
    for i in range(period, len(tr_list)):
        smooth_plus = smooth_plus - smooth_plus / period + plus_dm_list[i]
        smooth_minus = smooth_minus - smooth_minus / period + minus_dm_list[i]
        smooth_tr = smooth_tr - smooth_tr / period + tr_list[i]
        if smooth_tr == 0:
            continue
        plus_di = 100 * smooth_plus / smooth_tr
        minus_di = 100 * smooth_minus / smooth_tr
        di_sum = plus_di + minus_di
        if di_sum == 0:
            continue
        dx_list.append(100 * abs(plus_di - minus_di) / di_sum)
    if len(dx_list) < period:
        return sum(dx_list) / len(dx_list) if dx_list else 0
    adx = sum(dx_list[:period]) / period
    for dx in dx_list[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


# ================================================================
# SENTIMENT DATA — Real-Time Feeds
# ================================================================

_fear_greed_cache = {"value": 50, "label": "Neutral", "ts": 0}
_FEAR_GREED_TTL = 900  # 15 min


def get_fear_greed_index() -> dict:
    """Crypto Fear & Greed Index (0-100) from alternative.me. Cached 15 min."""
    global _fear_greed_cache
    now = time.time()
    if now - _fear_greed_cache["ts"] < _FEAR_GREED_TTL:
        return _fear_greed_cache
    try:
        r = _session.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        if r.status_code == 200:
            data = r.json()["data"][0]
            _fear_greed_cache = {
                "value": int(data["value"]),
                "label": data["value_classification"],
                "ts": now,
            }
    except Exception:
        pass
    return _fear_greed_cache


_trending_cache = {"coins": [], "ts": 0}
_TRENDING_TTL = 300  # 5 min


def get_trending_coins() -> list:
    """Trending coins from CoinGecko (free, no API key). Cached 5 min."""
    global _trending_cache
    now = time.time()
    if now - _trending_cache["ts"] < _TRENDING_TTL:
        return _trending_cache["coins"]
    try:
        r = _session.get("https://api.coingecko.com/api/v3/search/trending", timeout=8)
        if r.status_code == 200:
            coins = []
            for item in r.json().get("coins", []):
                c = item.get("item", {})
                coins.append({
                    "id": c.get("id", ""),
                    "symbol": c.get("symbol", "").upper(),
                    "name": c.get("name", ""),
                    "market_cap_rank": c.get("market_cap_rank", 9999),
                })
            _trending_cache = {"coins": coins, "ts": now}
    except Exception:
        pass
    return _trending_cache["coins"]


# ================================================================
# COMPOSITE SCORING ENGINE
# ================================================================

_score_cache = {}  # key: symbol -> {score, details..., ts}
_SCORE_TTL = 60


def _neutral_result() -> dict:
    return {
        "score": 50.0, "signal": "neutral",
        "rsi_1h": 50.0, "rsi_4h": 50.0,
        "macd_hist": 0, "bb_pct_b": 0.5,
        "ema_trend": "neutral", "adx": 0,
        "atr_pct": 0, "fear_greed": 50,
        "ts": time.time(),
    }


def get_ai_score(exchange, symbol: str) -> dict:
    """
    Compute composite AI confidence score (0-100) for a symbol.
    Thread-safe. Cached 60s. Falls back to neutral on error.
    """
    now = time.time()
    with _ai_lock:
        cached = _score_cache.get(symbol)
        if cached and now - cached["ts"] < _SCORE_TTL:
            return cached

    try:
        candles_1h = fetch_ohlcv_cached(exchange, symbol, "1h", 200)
        candles_4h = fetch_ohlcv_cached(exchange, symbol, "4h", 200)
        if not candles_1h or len(candles_1h) < 30:
            return _neutral_result()

        closes_1h = [c[4] for c in candles_1h]
        closes_4h = [c[4] for c in candles_4h] if candles_4h and len(candles_4h) > 15 else closes_1h
        price = closes_1h[-1]

        # --- Compute all indicators ---
        rsi_1h = compute_rsi(closes_1h, 14)
        rsi_4h = compute_rsi(closes_4h, 14)
        macd_data = compute_macd(closes_1h)
        bb_data = compute_bollinger(closes_1h, 20, 2.0)
        ema_12 = compute_ema(closes_1h, 12)
        ema_26 = compute_ema(closes_1h, 26)
        ema_50 = compute_ema(closes_1h, 50)
        ema_200 = compute_ema(closes_1h, 200) if len(closes_1h) >= 200 else ema_50
        adx_val = compute_adx(candles_1h, 14)
        atr_val = compute_atr(candles_1h, 14)
        atr_pct = (atr_val / price * 100) if price > 0 else 0
        fg = get_fear_greed_index()

        # === 1. RSI SCORE (weight: 20%) ===
        def rsi_to_score(rsi):
            if rsi < 25: return 90
            if rsi < 35: return 70
            if rsi < 45: return 55
            if rsi < 55: return 50
            if rsi < 65: return 45
            if rsi < 75: return 30
            return 10

        rsi_final = rsi_to_score(rsi_1h) * 0.6 + rsi_to_score(rsi_4h) * 0.4

        # === 2. MACD SCORE (weight: 20%) ===
        hist = macd_data["histogram"]
        macd_line = macd_data["macd"]
        signal_line = macd_data["signal"]
        if hist > 0 and macd_line > signal_line:
            macd_score = 70 + min(abs(hist) / (price * 0.001 + 1e-10), 20)
        elif hist > 0:
            macd_score = 60
        elif hist < 0 and macd_line < signal_line:
            macd_score = 30 - min(abs(hist) / (price * 0.001 + 1e-10), 20)
        elif hist < 0:
            macd_score = 40
        else:
            macd_score = 50
        macd_score = max(5, min(95, macd_score))

        # === 3. BOLLINGER BAND SCORE (weight: 15%) ===
        pct_b = bb_data["pct_b"]
        if pct_b < 0.05: bb_score = 85
        elif pct_b < 0.2: bb_score = 70
        elif pct_b < 0.4: bb_score = 55
        elif pct_b < 0.6: bb_score = 50
        elif pct_b < 0.8: bb_score = 45
        elif pct_b < 0.95: bb_score = 30
        else: bb_score = 15

        # === 4. EMA TREND SCORE (weight: 20%) ===
        ema12_val = ema_12[-1] if ema_12 else price
        ema26_val = ema_26[-1] if ema_26 else price
        ema50_val = ema_50[-1] if ema_50 else price
        ema200_val = ema_200[-1] if ema_200 else price
        bullish_count = sum([
            price > ema12_val,
            price > ema26_val,
            price > ema50_val,
            price > ema200_val,
            ema12_val > ema26_val,  # golden cross
            ema50_val > ema200_val,  # macro trend
        ])
        ema_score = 20 + bullish_count * 10  # 20 to 80
        ema_trend = "bullish" if bullish_count >= 5 else ("bearish" if bullish_count <= 1 else "neutral")

        # === 5. ADX SCORE (weight: 10%) ===
        if adx_val > 40: adx_score = 70
        elif adx_val > 25: adx_score = 60
        elif adx_val < 15: adx_score = 35
        else: adx_score = 50

        # === 6. FEAR & GREED SCORE (weight: 15%) ===
        fg_val = fg["value"]
        if fg_val < 15: fg_score = 80
        elif fg_val < 30: fg_score = 65
        elif fg_val < 50: fg_score = 55
        elif fg_val < 70: fg_score = 45
        elif fg_val < 85: fg_score = 35
        else: fg_score = 20

        # === WEIGHTED COMPOSITE ===
        composite = (
            rsi_final * 0.20 +
            macd_score * 0.20 +
            bb_score * 0.15 +
            ema_score * 0.20 +
            adx_score * 0.10 +
            fg_score * 0.15
        )
        composite = max(0, min(100, composite))

        # Map to signal label
        if composite <= 30: signal = "strong_sell"
        elif composite <= 45: signal = "sell"
        elif composite <= 55: signal = "neutral"
        elif composite <= 70: signal = "buy"
        else: signal = "strong_buy"

        result = {
            "score": round(composite, 1),
            "signal": signal,
            "rsi_1h": round(rsi_1h, 1),
            "rsi_4h": round(rsi_4h, 1),
            "macd_hist": round(macd_data["histogram"], 6),
            "bb_pct_b": round(bb_data["pct_b"], 3),
            "ema_trend": ema_trend,
            "adx": round(adx_val, 1),
            "atr_pct": round(atr_pct, 2),
            "fear_greed": fg_val,
            "ts": now,
        }
        with _ai_lock:
            _score_cache[symbol] = result
        return result

    except Exception:
        return _neutral_result()


# ================================================================
# HELPER FUNCTIONS FOR WORKERS
# ================================================================

def get_fear_greed_multiplier() -> float:
    """Position sizing multiplier. Extreme fear = 1.3x, extreme greed = 0.6x."""
    fg = get_fear_greed_index()["value"]
    if fg < 20: return 1.3
    if fg < 40: return 1.1
    if fg < 60: return 1.0
    if fg < 80: return 0.8
    return 0.6


def get_trending_symbols() -> list:
    """Return USDT pair symbols for CoinGecko trending coins."""
    trending = get_trending_coins()
    return [f"{c['symbol']}/USDT" for c in trending if c.get("symbol")]


def should_take_partial_profit(exchange, symbol: str) -> bool:
    """True if RSI > 75 on 1h — signal to take partial profits."""
    score_data = get_ai_score(exchange, symbol)
    return score_data["rsi_1h"] > 75


def get_ai_trend(exchange) -> str:
    """Multi-indicator BTC trend. Replaces simple get_trend_filter()."""
    score = get_ai_score(exchange, "BTC/USDT")
    if score["score"] >= 60 and score["ema_trend"] == "bullish":
        return "up"
    if score["score"] <= 40 and score["ema_trend"] == "bearish":
        return "down"
    return "neutral"


def get_dashboard_ai_data(exchange, symbols: list) -> dict:
    """Return AI data for dashboard consumption."""
    fg = get_fear_greed_index()
    trending = get_trending_coins()
    scores = {}
    for sym in symbols[:20]:
        try:
            s = get_ai_score(exchange, sym)
            scores[sym] = {"score": s["score"], "signal": s["signal"]}
        except Exception:
            pass
    return {
        "fear_greed": {"value": fg["value"], "label": fg["label"]},
        "trending": [{"symbol": c["symbol"], "name": c["name"]} for c in trending[:7]],
        "scores": scores,
    }
