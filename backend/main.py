from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import yfinance as yf
import pandas as pd
import sqlite3
import json
from datetime import datetime

app = FastAPI(title="TradeSim API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "tradesim.db"
INITIAL_BALANCE = 10000.0
SLIPPAGE = 0.0005  # 0.05%

# ── NSE / BSE suffix map ───────────────────────────────────────────────
EXCHANGE_SUFFIX = {
    "NSE": ".NS",
    "BSE": ".BO",
    "US":  "",
}

# FIX: Replaced BSE numeric codes with named tickers that yfinance supports
POPULAR_SYMBOLS = {
    "NSE": ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
            "HINDUNILVR", "SBIN", "BAJFINANCE", "WIPRO", "ADANIENT"],
    "BSE": ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
            "HINDUNILVR", "SBIN", "BAJFINANCE", "WIPRO", "ADANIENT"],
    "US":  ["AAPL", "TSLA", "MSFT", "GOOGL", "AMZN",
            "META", "NVDA", "BTC-USD", "ETH-USD", "SPY"],
}


# ── DB helpers ─────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS wallet (
            id INTEGER PRIMARY KEY DEFAULT 1,
            cash REAL NOT NULL DEFAULT 10000.0
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            exchange TEXT NOT NULL DEFAULT 'US',
            action TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL NOT NULL,
            slippage_price REAL NOT NULL,
            timestamp TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            exchange TEXT NOT NULL DEFAULT 'US',
            qty REAL NOT NULL DEFAULT 0,
            total_cost REAL NOT NULL DEFAULT 0,
            UNIQUE(symbol, exchange)
        );
    """)
    cur.execute("INSERT OR IGNORE INTO wallet (id, cash) VALUES (1, ?)", (INITIAL_BALANCE,))
    conn.commit()
    conn.close()

init_db()


# ── Ticker helper ──────────────────────────────────────────────────────
def build_ticker(symbol: str, exchange: str) -> str:
    suffix = EXCHANGE_SUFFIX.get(exchange.upper(), "")
    return f"{symbol.upper()}{suffix}"


def fetch_current_price(symbol: str, exchange: str) -> float:
    ticker = build_ticker(symbol, exchange)
    t = yf.Ticker(ticker)
    hist = t.history(period="2d")

    if hist.empty and exchange.upper() == "BSE":
        fallback = f"{symbol.upper()}.NS"
        t = yf.Ticker(fallback)
        hist = t.history(period="2d")

    if hist.empty:
        raise HTTPException(status_code=404, detail=f"No data for {ticker}")
    return float(hist["Close"].iloc[-1])


# ── Market data endpoints ──────────────────────────────────────────────
@app.get("/api/chart/{exchange}/{symbol}")
def get_chart(exchange: str, symbol: str, period: str = "6mo"):
    ticker = build_ticker(symbol, exchange)
    t = yf.Ticker(ticker)
    hist = t.history(period=period)

    # FIX: Fallback to NSE if BSE returns empty
    if hist.empty and exchange.upper() == "BSE":
        ticker = f"{symbol.upper()}.NS"
        hist = yf.Ticker(ticker).history(period=period)

    if hist.empty:
        raise HTTPException(status_code=404, detail=f"No data for {ticker}")

    hist = hist.reset_index()
    hist["Date"] = hist["Date"].astype(str).str[:10]

    # SMA-20
    hist["SMA20"] = hist["Close"].rolling(20).mean()

    # RSI-14
    delta = hist["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    hist["RSI14"] = 100 - (100 / (1 + rs))

    candles = []
    for _, row in hist.iterrows():
        candles.append({
            "time":   row["Date"],
            "open":   round(row["Open"], 4),
            "high":   round(row["High"], 4),
            "low":    round(row["Low"], 4),
            "close":  round(row["Close"], 4),
            "volume": int(row["Volume"]),
            "sma20":  None if pd.isna(row["SMA20"]) else round(row["SMA20"], 4),
            "rsi14":  None if pd.isna(row["RSI14"]) else round(row["RSI14"], 2),
        })
    return {"ticker": ticker, "data": candles}


@app.get("/api/price/{exchange}/{symbol}")
def get_price(exchange: str, symbol: str):
    price = fetch_current_price(symbol, exchange)
    return {"symbol": symbol, "exchange": exchange, "price": price}


# FIX: Removed duplicate broken route that called non-existent fetch_symbols_for_exchange()
@app.get("/api/symbols/{exchange}")
def get_symbols(exchange: str):
    syms = POPULAR_SYMBOLS.get(exchange.upper(), [])
    return {"exchange": exchange, "symbols": syms}


# ── Wallet & portfolio ─────────────────────────────────────────────────
@app.get("/api/wallet")
def get_wallet():
    conn = get_db()
    cur = conn.cursor()
    wallet = cur.execute("SELECT cash FROM wallet WHERE id=1").fetchone()
    positions = cur.execute("SELECT * FROM positions WHERE qty > 0").fetchall()

    holdings = []
    total_position_value = 0.0
    for pos in positions:
        sym = pos["symbol"]
        exc = pos["exchange"]
        try:
            cur_price = fetch_current_price(sym, exc)
        except:
            cur_price = 0.0
        avg_entry = pos["total_cost"] / pos["qty"] if pos["qty"] else 0
        unreal_pl = (cur_price - avg_entry) * pos["qty"]
        pos_value = cur_price * pos["qty"]
        total_position_value += pos_value
        holdings.append({
            "symbol":        sym,
            "exchange":      exc,
            "qty":           pos["qty"],
            "avg_entry":     round(avg_entry, 4),
            "current_price": round(cur_price, 4),
            "position_value":round(pos_value, 4),
            "unrealized_pl": round(unreal_pl, 4),
        })
    conn.close()
    cash = wallet["cash"]
    return {
        "cash":     round(cash, 2),
        "equity":   round(cash + total_position_value, 2),
        "holdings": holdings,
    }


# ── Order execution ────────────────────────────────────────────────────
class OrderRequest(BaseModel):
    symbol: str
    exchange: str = "US"
    action: str     # "buy" | "sell"
    qty: float
    use_slippage: bool = True

@app.post("/api/order")
def place_order(order: OrderRequest):
    if order.action.lower() not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="action must be 'buy' or 'sell'")
    if order.qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be positive")

    market_price = fetch_current_price(order.symbol, order.exchange)
    slip = SLIPPAGE if order.use_slippage else 0
    if order.action.lower() == "buy":
        exec_price = market_price * (1 + slip)
    else:
        exec_price = market_price * (1 - slip)

    conn = get_db()
    cur = conn.cursor()

    wallet = cur.execute("SELECT cash FROM wallet WHERE id=1").fetchone()
    cash = wallet["cash"]
    sym = order.symbol.upper()
    exc = order.exchange.upper()

    if order.action.lower() == "buy":
        cost = exec_price * order.qty
        if cost > cash:
            conn.close()
            raise HTTPException(status_code=400, detail=f"Insufficient balance. Need ₹{cost:.2f}, have ₹{cash:.2f}")
        cur.execute("UPDATE wallet SET cash = cash - ? WHERE id=1", (cost,))
        cur.execute("""
            INSERT INTO positions (symbol, exchange, qty, total_cost)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol, exchange) DO UPDATE SET
                qty        = qty + excluded.qty,
                total_cost = total_cost + excluded.total_cost
        """, (sym, exc, order.qty, exec_price * order.qty))
    else:
        pos = cur.execute(
            "SELECT qty FROM positions WHERE symbol=? AND exchange=?", (sym, exc)
        ).fetchone()
        if not pos or pos["qty"] < order.qty:
            conn.close()
            have = pos["qty"] if pos else 0
            raise HTTPException(status_code=400, detail=f"Insufficient position. Have {have}, need {order.qty}")
        proceeds = exec_price * order.qty
        cur.execute("UPDATE wallet SET cash = cash + ? WHERE id=1", (proceeds,))
        cur.execute("""
            UPDATE positions
            SET qty        = qty - ?,
                total_cost = total_cost - (total_cost / qty * ?)
            WHERE symbol=? AND exchange=?
        """, (order.qty, order.qty, sym, exc))

    cur.execute("""
        INSERT INTO trades (symbol, exchange, action, qty, price, slippage_price, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (sym, exc, order.action.lower(), order.qty, market_price, exec_price,
          datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    return {"status": "ok", "exec_price": round(exec_price, 4), "market_price": round(market_price, 4)}


# ── Trade history ──────────────────────────────────────────────────────
@app.get("/api/trades")
def get_trades():
    conn = get_db()
    cur = conn.cursor()
    rows = cur.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return {"trades": [dict(r) for r in rows]}


# ── Reset ──────────────────────────────────────────────────────────────
@app.post("/api/reset")
def reset():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript("""
        UPDATE wallet SET cash = 10000.0 WHERE id = 1;
        DELETE FROM trades;
        DELETE FROM positions;
    """)
    conn.commit()
    conn.close()
    return {"status": "reset", "balance": 10000.0}


# ── /buy and /sell alias routes (Sprint 3 / B4 spec) ──────────────────
class SimpleOrder(BaseModel):
    symbol: str
    exchange: str = "US"
    qty: float
    use_slippage: bool = True

@app.post("/api/buy")
def buy(order: SimpleOrder):
    return place_order(OrderRequest(
        symbol=order.symbol, exchange=order.exchange,
        action="buy", qty=order.qty, use_slippage=order.use_slippage
    ))

@app.post("/api/sell")
def sell(order: SimpleOrder):
    return place_order(OrderRequest(
        symbol=order.symbol, exchange=order.exchange,
        action="sell", qty=order.qty, use_slippage=order.use_slippage
    ))


# ── Symbol validation endpoint ─────────────────────────────────────────
@app.get("/api/validate/{exchange}/{symbol}")
def validate_symbol(exchange: str, symbol: str):
    """Returns whether the symbol is valid and fetchable."""
    ticker = build_ticker(symbol, exchange)
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="5d")
        if hist.empty:
            return {"valid": False, "ticker": ticker, "reason": "No data returned for this symbol."}
        price = float(hist["Close"].iloc[-1])
        return {"valid": True, "ticker": ticker, "price": round(price, 4)}
    except Exception as e:
        return {"valid": False, "ticker": ticker, "reason": str(e)}