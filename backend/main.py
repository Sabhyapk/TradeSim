from fastapi import FastAPI, HTTPException, Cookie, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
import yfinance as yf
import pandas as pd
import sqlite3
import secrets
import hashlib
import re
import os
import requests
from datetime import datetime
from typing import Optional

app = FastAPI(title="TradeSim API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "https://trade-sim-tau.vercel.app",],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH         = "tradesim.db"
YF_CACHE_DIR    = os.path.join(os.path.dirname(__file__), "yfinance_cache")
INITIAL_BALANCE = 10000.0
SLIPPAGE        = 0.0005   # 0.05%
SESSION_DAYS    = 7

# ── Exchange suffix map ────────────────────────────────────────────────
EXCHANGE_SUFFIX = {"NSE": ".NS", "BSE": ".BO", "US": ""}

POPULAR_SYMBOLS = {
    "NSE": ["RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK",
            "HINDUNILVR","SBIN","BAJFINANCE","WIPRO","ADANIENT"],
    "BSE": ["RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK",
            "HINDUNILVR","SBIN","BAJFINANCE","WIPRO","ADANIENT"],
    "US":  ["AAPL","TSLA","MSFT","GOOGL","AMZN",
            "META","NVDA","BTC-USD","ETH-USD","SPY"],
}

# Intraday intervals use a short window to stay inside Yahoo Finance limits.
INTRADAY = {"1m", "5m", "10m", "1h"}
MAX_CHART_DAYS = 100
MAX_INTRADAY_DAYS = 7

 

INTERVAL_PERIOD_MAP = {
    "1d":  f"{MAX_CHART_DAYS}d",
    "1mo": f"{MAX_CHART_DAYS}d",
    "3mo": f"{MAX_CHART_DAYS}d",
}

os.makedirs(YF_CACHE_DIR, exist_ok=True)
yf.set_tz_cache_location(YF_CACHE_DIR)

# ── Password helpers ───────────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt   = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{hashed}"

def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hashed = stored.split(":")
        return hashlib.sha256((salt + password).encode()).hexdigest() == hashed
    except Exception:
        return False

# ── DB helpers ─────────────────────────────────────────────────────────
# def get_db():
#     conn = sqlite3.connect(DB_PATH, check_same_thread=False)
#     conn.row_factory = sqlite3.Row
#     return conn

def get_db():
    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False,
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            email    TEXT    NOT NULL UNIQUE,
            password TEXT    NOT NULL,
            created  TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token    TEXT    PRIMARY KEY,
            user_id  INTEGER NOT NULL,
            created  TEXT    NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS wallet (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            cash    REAL    NOT NULL DEFAULT 10000.0,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS trades (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL,
            symbol         TEXT    NOT NULL,
            exchange       TEXT    NOT NULL DEFAULT 'US',
            action         TEXT    NOT NULL,
            qty            REAL    NOT NULL,
            price          REAL    NOT NULL,
            slippage_price REAL    NOT NULL,
            timestamp      TEXT    NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS positions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            symbol     TEXT    NOT NULL,
            exchange   TEXT    NOT NULL DEFAULT 'US',
            qty        REAL    NOT NULL DEFAULT 0,
            total_cost REAL    NOT NULL DEFAULT 0,
            UNIQUE(user_id, symbol, exchange),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ── Session helper ─────────────────────────────────────────────────────
def get_current_user(session_token: Optional[str] = Cookie(default=None)) -> int:
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    conn = get_db()
    row  = conn.execute(
        "SELECT user_id FROM sessions WHERE token=?", (session_token,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return row["user_id"]

# ── Ticker helpers ─────────────────────────────────────────────────────
def build_ticker(symbol: str, exchange: str) -> str:
    return f"{symbol.upper()}{EXCHANGE_SUFFIX.get(exchange.upper(), '')}"

def normalize_chart_period(period: str, intraday: bool) -> str:
    match = re.fullmatch(r"(\d+)d", (period or "").strip().lower())
    requested_days = int(match.group(1)) if match else MAX_CHART_DAYS
    max_days = MAX_INTRADAY_DAYS if intraday else MAX_CHART_DAYS
    return f"{max(1, min(requested_days, max_days))}d"

def yahoo_chart_history(ticker: str, period: str, interval: str) -> pd.DataFrame:
    yahoo_interval = "60m" if interval == "1h" else interval
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    response = requests.get(
        url,
        params={
            "range": period,
            "interval": yahoo_interval,
            "includePrePost": "false",
            "events": "div,splits",
        },
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()["chart"]["result"]
    if not payload:
        return pd.DataFrame()

    result = payload[0]
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    if not timestamps or not quote:
        return pd.DataFrame()

    tz = result.get("meta", {}).get("exchangeTimezoneName")
    date_index = pd.to_datetime(timestamps, unit="s", utc=True)
    if tz:
        date_index = date_index.tz_convert(tz)

    hist = pd.DataFrame({
        "Open": quote.get("open"),
        "High": quote.get("high"),
        "Low": quote.get("low"),
        "Close": quote.get("close"),
        "Volume": quote.get("volume"),
    }, index=date_index)
    hist.index.name = "Datetime" if interval in INTRADAY or interval in {"2m", "5m", "15m", "30m", "60m", "90m", "1h", "4h"} else "Date"
    return hist.dropna(subset=["Open", "High", "Low", "Close"])

def fetch_history(ticker: str, period: str, interval: str) -> pd.DataFrame:
    try:
        hist = yahoo_chart_history(ticker, period, interval)
        if not hist.empty:
            return hist
    except Exception:
        pass
    return yf.Ticker(ticker).history(period=period, interval=interval)

def fetch_current_price(symbol: str, exchange: str) -> float:
    ticker = build_ticker(symbol, exchange)
    hist = fetch_history(ticker, "5d", "1d")
    if hist.empty and exchange.upper() == "BSE":
        hist = fetch_history(f"{symbol.upper()}.NS", "5d", "1d")
    if not hist.empty:
        return float(hist["Close"].iloc[-1])

    ticker_obj = yf.Ticker(ticker)
    try:
        fast_price = ticker_obj.fast_info.get("last_price")
        if fast_price:
            return float(fast_price)
    except Exception:
        pass

    hist = ticker_obj.history(period="2d", interval="1m")
    if hist.empty:
        hist = ticker_obj.history(period="2d")
    if hist.empty and exchange.upper() == "BSE":
        fallback_obj = yf.Ticker(f"{symbol.upper()}.NS")
        hist = fallback_obj.history(period="2d", interval="1m")
        if hist.empty:
            hist = fallback_obj.history(period="2d")
    if hist.empty:
        raise HTTPException(status_code=404, detail=f"No data for {ticker}")
    return float(hist["Close"].iloc[-1])

# ── Indicator engine ───────────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    vol   = df["Volume"]

    # Trend
    df["SMA20"] = close.rolling(20).mean()
    df["EMA20"] = close.ewm(span=20, adjust=False).mean()
    df["EMA50"] = close.ewm(span=50, adjust=False).mean()

    # Bollinger Bands (20, 2σ)
    bb_mid         = close.rolling(20).mean()
    bb_std         = close.rolling(20).std()
    df["BB_upper"] = bb_mid + 2 * bb_std
    df["BB_mid"]   = bb_mid
    df["BB_lower"] = bb_mid - 2 * bb_std

    # RSI-14
    delta        = close.diff()
    gain         = delta.clip(lower=0).rolling(14).mean()
    loss         = (-delta.clip(upper=0)).rolling(14).mean()
    df["RSI14"]  = 100 - (100 / (1 + gain / loss))

    # MACD (12, 26, 9)
    ema12              = close.ewm(span=12, adjust=False).mean()
    ema26              = close.ewm(span=26, adjust=False).mean()
    df["MACD"]         = ema12 - ema26
    df["MACD_signal"]  = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"]    = df["MACD"] - df["MACD_signal"]

    # VWAP (cumulative — most meaningful on intraday)
    tp            = (high + low + close) / 3
    df["VWAP"]    = (tp * vol).cumsum() / vol.cumsum()

    # Stochastic RSI (14, 3, 3)
    rsi                = df["RSI14"]
    rsi_min            = rsi.rolling(14).min()
    rsi_max            = rsi.rolling(14).max()
    stoch              = (rsi - rsi_min) / (rsi_max - rsi_min + 1e-9)
    df["StochRSI_K"]   = stoch.rolling(3).mean() * 100
    df["StochRSI_D"]   = df["StochRSI_K"].rolling(3).mean()

    return df

def _f(val) -> Optional[float]:
    """Safe round — returns None for NaN."""
    return None if pd.isna(val) else round(float(val), 4)

# ═══════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[\w\.\+\-]+@[\w\-]+\.[a-z]{2,}$", v):
            raise ValueError("Invalid email address")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not re.search(r"[0-9]", v):
            raise ValueError("Password must contain at least one digit")
        return v

class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def normalise(cls, v: str) -> str:
        return v.strip().lower()

def _make_session(user_id: int, conn) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created) VALUES (?, ?, ?)",
        (token, user_id, datetime.utcnow().isoformat())
    )
    return token

def _set_cookie(response: Response, token: str):
    response.set_cookie(
        key="session_token", value=token,
        httponly=True, max_age=SESSION_DAYS * 86400, samesite="lax"
    )

@app.post("/api/auth/register")
def register(req: RegisterRequest, response: Response):
    conn = get_db()
    if conn.execute("SELECT id FROM users WHERE email=?", (req.email,)).fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="Email already registered")

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (email, password, created) VALUES (?, ?, ?)",
        (req.email, hash_password(req.password), datetime.utcnow().isoformat())
    )
    user_id = cur.lastrowid
    cur.execute("INSERT INTO wallet (user_id, cash) VALUES (?, ?)", (user_id, INITIAL_BALANCE))
    token = _make_session(user_id, conn)
    conn.commit()
    conn.close()
    _set_cookie(response, token)
    return {"status": "registered", "email": req.email}

@app.post("/api/auth/login")
def login(req: LoginRequest, response: Response):
    conn = get_db()
    user = conn.execute(
        "SELECT id, password FROM users WHERE email=?", (req.email,)
    ).fetchone()
    if not user or not verify_password(req.password, user["password"]):
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = _make_session(user["id"], conn)
    conn.commit()
    conn.close()
    _set_cookie(response, token)
    return {"status": "ok", "email": req.email}

@app.post("/api/auth/logout")
def logout(response: Response, session_token: Optional[str] = Cookie(default=None)):
    if session_token:
        conn = get_db()
        conn.execute("DELETE FROM sessions WHERE token=?", (session_token,))
        conn.commit()
        conn.close()
    response.delete_cookie("session_token")
    return {"status": "logged out"}

@app.get("/api/auth/me")
def me(session_token: Optional[str] = Cookie(default=None)):
    user_id = get_current_user(session_token)
    conn    = get_db()
    user    = conn.execute(
        "SELECT email, created FROM users WHERE id=?", (user_id,)
    ).fetchone()
    conn.close()
    return {"user_id": user_id, "email": user["email"], "created": user["created"]}

# ═══════════════════════════════════════════════════════════════════════
# MARKET DATA  (public — no auth needed)
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/symbols/{exchange}")
def get_symbols(exchange: str):
    return {"exchange": exchange, "symbols": POPULAR_SYMBOLS.get(exchange.upper(), [])}

@app.get("/api/price/{exchange}/{symbol}")
def get_price(exchange: str, symbol: str):
    price = fetch_current_price(symbol, exchange)
    return {"symbol": symbol, "exchange": exchange, "price": price}

@app.get("/api/chart/{exchange}/{symbol}")
def get_chart(
    exchange: str,
    symbol:   str,
    interval: str = "1d",    # 1m | 5m | 10m | 1h | 1d | 1mo | 3mo
    period:   str = "100d",  # capped at 100d; intraday capped lower
):
    ticker_str   = build_ticker(symbol, exchange)
    resample_10m = False
    yf_interval  = interval

    VALID_INTERVALS = {
    "1m", "2m", "5m", "10m", "15m", "30m",
    "60m", "90m", "1h", "4h",
    "1d", "5d", "1wk", "1mo", "3mo"
    }

    if interval not in VALID_INTERVALS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid interval {interval}"
        ) 

    if interval == "10m":
        yf_interval  = "5m"   # fetch 5m, resample to 10m
        resample_10m = True

    fetch_period = normalize_chart_period(
        period if interval not in INTERVAL_PERIOD_MAP else period or INTERVAL_PERIOD_MAP[interval],
        interval in INTRADAY,
    )

    hist = fetch_history(ticker_str, fetch_period, yf_interval)

    # BSE fallback
    if hist.empty and exchange.upper() == "BSE":
        ticker_str = f"{symbol.upper()}.NS"
        hist       = fetch_history(ticker_str, fetch_period, yf_interval)

    if hist.empty:
        raise HTTPException(status_code=404, detail=f"No data for {ticker_str}")

    # Resample 5m → 10m
    if resample_10m:
        hist = hist.resample("10min").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        ).dropna()

    hist = hist.reset_index()

    # Intraday → "Datetime" column; daily → "Date"
    date_col    = "Datetime" if "Datetime" in hist.columns else "Date"
    hist["_ts"] = hist[date_col].astype(str)

    hist = compute_indicators(hist)

    candles = []
    for _, row in hist.iterrows():
        candles.append({
            "time":        row["_ts"],
            "open":        _f(row["Open"]),
            "high":        _f(row["High"]),
            "low":         _f(row["Low"]),
            "close":       _f(row["Close"]),
            "volume":      int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
            # Trend overlays
            "sma20":       _f(row["SMA20"]),
            "ema20":       _f(row["EMA20"]),
            "ema50":       _f(row["EMA50"]),
            "vwap":        _f(row["VWAP"]),
            # Bollinger Bands
            "bb_upper":    _f(row["BB_upper"]),
            "bb_mid":      _f(row["BB_mid"]),
            "bb_lower":    _f(row["BB_lower"]),
            # Oscillators
            "rsi14":       _f(row["RSI14"]),
            "macd":        _f(row["MACD"]),
            "macd_signal": _f(row["MACD_signal"]),
            "macd_hist":   _f(row["MACD_hist"]),
            "stoch_rsi_k": _f(row["StochRSI_K"]),
            "stoch_rsi_d": _f(row["StochRSI_D"]),
        })

    return {"ticker": ticker_str, "interval": interval, "data": candles}

@app.get("/api/validate/{exchange}/{symbol}")
def validate_symbol(exchange: str, symbol: str):
    ticker = build_ticker(symbol, exchange)
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        if hist.empty:
            return {"valid": False, "ticker": ticker, "reason": "No data returned"}
        return {"valid": True, "ticker": ticker, "price": round(float(hist["Close"].iloc[-1]), 4)}
    except Exception as e:
        return {"valid": False, "ticker": ticker, "reason": str(e)}

# ═══════════════════════════════════════════════════════════════════════
# WALLET & PORTFOLIO  (auth required)
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/wallet")
def get_wallet(session_token: Optional[str] = Cookie(default=None)):
    user_id   = get_current_user(session_token)
    conn      = get_db()
    wallet    = conn.execute("SELECT cash FROM wallet WHERE user_id=?", (user_id,)).fetchone()
    positions = conn.execute(
        "SELECT * FROM positions WHERE user_id=? AND qty > 0", (user_id,)
    ).fetchall()

    holdings, total_pos_val = [], 0.0
    for pos in positions:
        sym, exc = pos["symbol"], pos["exchange"]
        try:
            cur_price = fetch_current_price(sym, exc)
        except Exception:
            cur_price = 0.0
        avg_entry = pos["total_cost"] / pos["qty"] if pos["qty"] else 0
        pos_value = cur_price * pos["qty"]
        total_pos_val += pos_value
        holdings.append({
            "symbol":         sym,
            "exchange":       exc,
            "qty":            pos["qty"],
            "avg_entry":      round(avg_entry, 4),
            "current_price":  round(cur_price, 4),
            "position_value": round(pos_value, 4),
            "unrealized_pl":  round((cur_price - avg_entry) * pos["qty"], 4),
        })
    conn.close()
    cash = wallet["cash"]
    return {"cash": round(cash, 2), "equity": round(cash + total_pos_val, 2), "holdings": holdings}

# ═══════════════════════════════════════════════════════════════════════
# ORDER EXECUTION  (auth required)
# ═══════════════════════════════════════════════════════════════════════

class OrderRequest(BaseModel):
    symbol: str
    exchange: str = "US"
    action: str
    qty: float
    use_slippage: bool = True

@app.post("/api/order")
def place_order(order: OrderRequest, session_token: Optional[str] = Cookie(default=None)):
    user_id = get_current_user(session_token)

    if order.action.lower() not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="action must be 'buy' or 'sell'")
    if order.qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be positive")
        
    # Add this validation for fractional shares on NSE and BSE
    if order.exchange.upper() in ["NSE", "BSE"] and not float(order.qty).is_integer():
        raise HTTPException(status_code=400, detail="Fractional units are not allowed for NSE and BSE")

    market_price = fetch_current_price(order.symbol, order.exchange)
    slip         = SLIPPAGE if order.use_slippage else 0
    exec_price   = market_price * (1 + slip) if order.action.lower() == "buy" \
                   else market_price * (1 - slip)

    conn = get_db()
    cur  = conn.cursor()
    cash = cur.execute("SELECT cash FROM wallet WHERE user_id=?", (user_id,)).fetchone()["cash"]
    sym  = order.symbol.upper()
    exc  = order.exchange.upper()

    if order.action.lower() == "buy":
        cost = exec_price * order.qty
        if cost > cash:
            conn.close()
            raise HTTPException(status_code=400,
                detail=f"Insufficient balance. Need {cost:.2f}, have {cash:.2f}")
        cur.execute("UPDATE wallet SET cash = cash - ? WHERE user_id=?", (cost, user_id))
        cur.execute("""
            INSERT INTO positions (user_id, symbol, exchange, qty, total_cost)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, symbol, exchange) DO UPDATE SET
                qty        = qty + excluded.qty,
                total_cost = total_cost + excluded.total_cost
        """, (user_id, sym, exc, order.qty, exec_price * order.qty))
    else:
        pos = cur.execute(
            "SELECT qty FROM positions WHERE user_id=? AND symbol=? AND exchange=?",
            (user_id, sym, exc)
        ).fetchone()
        if not pos or pos["qty"] < order.qty:
            conn.close()
            raise HTTPException(status_code=400,
                detail=f"Insufficient position. Have {pos['qty'] if pos else 0}, need {order.qty}")
        cur.execute("UPDATE wallet SET cash = cash + ? WHERE user_id=?",
                    (exec_price * order.qty, user_id))
        cur.execute("""
            UPDATE positions
            SET qty        = qty - ?,
                total_cost = total_cost - (total_cost / qty * ?)
            WHERE user_id=? AND symbol=? AND exchange=?
        """, (order.qty, order.qty, user_id, sym, exc))

    cur.execute("""
        INSERT INTO trades (user_id, symbol, exchange, action, qty, price, slippage_price, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, sym, exc, order.action.lower(), order.qty,
          market_price, exec_price, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    return {"status": "ok", "exec_price": round(exec_price, 4), "market_price": round(market_price, 4)}

class SimpleOrder(BaseModel):
    symbol: str
    exchange: str = "US"
    qty: float
    use_slippage: bool = True

@app.post("/api/buy")
def buy(order: SimpleOrder, session_token: Optional[str] = Cookie(default=None)):
    return place_order(
        OrderRequest(symbol=order.symbol, exchange=order.exchange,
                     action="buy", qty=order.qty, use_slippage=order.use_slippage),
        session_token=session_token
    )

@app.post("/api/sell")
def sell(order: SimpleOrder, session_token: Optional[str] = Cookie(default=None)):
    return place_order(
        OrderRequest(symbol=order.symbol, exchange=order.exchange,
                     action="sell", qty=order.qty, use_slippage=order.use_slippage),
        session_token=session_token
    )

# ═══════════════════════════════════════════════════════════════════════
# TRADE HISTORY & RESET  (auth required)
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/trades")
def get_trades(session_token: Optional[str] = Cookie(default=None)):
    user_id = get_current_user(session_token)
    conn    = get_db()
    rows    = conn.execute(
        "SELECT * FROM trades WHERE user_id=? ORDER BY id DESC LIMIT 100", (user_id,)
    ).fetchall()
    conn.close()
    return {"trades": [dict(r) for r in rows]}

@app.post("/api/reset")
def reset(session_token: Optional[str] = Cookie(default=None)):
    user_id = get_current_user(session_token)
    conn    = get_db()
    conn.execute("UPDATE wallet   SET cash=? WHERE user_id=?", (INITIAL_BALANCE, user_id))
    conn.execute("DELETE FROM trades    WHERE user_id=?",      (user_id,))
    conn.execute("DELETE FROM positions WHERE user_id=?",      (user_id,))
    conn.commit()
    conn.close()
    return {"status": "reset", "balance": INITIAL_BALANCE}
