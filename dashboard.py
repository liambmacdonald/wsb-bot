"""
WSB Bot web dashboard — Apple-inspired single-page app on port 5000.
Call dashboard.start() from main.py to launch in a background thread.
Call dashboard.set_last_run() after each pipeline cycle to update the status pill.
"""

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import yfinance as yf
from flask import Flask, jsonify, Response

from config import DB_PATH

app = Flask(__name__)
_last_run: datetime | None = None

_price_cache: list = []
_price_cache_ts: float = 0.0
_price_lock = threading.Lock()


def set_last_run() -> None:
    global _last_run
    _last_run = datetime.now(timezone.utc)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _fetch_price(ticker: str) -> tuple[str, float | None]:
    try:
        fi = yf.Ticker(ticker).fast_info
        for attr in ("last_price", "lastPrice", "regular_market_price", "regularMarketPrice"):
            price = getattr(fi, attr, None)
            if price and float(price) > 0:
                return ticker, float(price)
    except Exception:
        pass
    try:
        hist = yf.Ticker(ticker).history(period="1d", interval="5m")
        if not hist.empty:
            return ticker, float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return ticker, None


# ── API ────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")


@app.route("/api/stats")
def api_stats():
    conn = _db()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM performance").fetchall()]
    except Exception:
        rows = []
    finally:
        conn.close()

    total = len(rows)

    resolved_6h = [r for r in rows if r.get("percent_change_6h") is not None]
    wins_6h = sum(1 for r in resolved_6h if r.get("outcome_6h") == "WIN")
    win_rate_6h = round(wins_6h / len(resolved_6h) * 100, 1) if resolved_6h else None

    resolved_24h = [r for r in rows if r.get("percent_change_24h") is not None]
    wins_24h = sum(1 for r in resolved_24h if r.get("outcome") == "WIN")
    win_rate_24h = round(wins_24h / len(resolved_24h) * 100, 1) if resolved_24h else None

    best_call = None
    def _pct(r: dict) -> float:
        return r.get("percent_change_24h") or r.get("percent_change_6h") or 0.0

    resolved_any = [r for r in rows if r.get("percent_change_24h") is not None or r.get("percent_change_6h") is not None]
    if resolved_any:
        best = max(resolved_any, key=_pct)
        pct = _pct(best)
        if pct > 0:
            best_call = {"ticker": best["ticker"], "pct": round(pct, 2), "signal": best["signal"]}

    now = datetime.now(timezone.utc)
    last_updated = _last_run.strftime("%Y-%m-%d %H:%M UTC") if _last_run else "Never"
    bot_running = _last_run is not None and (now - _last_run).total_seconds() < 3600

    return jsonify({
        "total_signals": total,
        "win_rate_6h": win_rate_6h,
        "win_rate_24h": win_rate_24h,
        "best_call": best_call,
        "last_updated": last_updated,
        "bot_running": bot_running,
    })


@app.route("/api/performance")
def api_performance():
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM performance ORDER BY signal_timestamp DESC LIMIT 100"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])
    finally:
        conn.close()


@app.route("/api/options")
def api_options():
    conn = _db()
    try:
        recs = conn.execute("""
            SELECT * FROM performance
            WHERE option_contract IS NOT NULL
            ORDER BY signal_timestamp DESC
            LIMIT 15
        """).fetchall()
        warnings = conn.execute("""
            SELECT * FROM performance
            WHERE option_iv IS NOT NULL
              AND option_iv > 0.8
              AND outcome = 'PENDING'
            ORDER BY option_iv DESC
            LIMIT 10
        """).fetchall()
        return jsonify({
            "recommendations": [dict(r) for r in recs],
            "iv_warnings": [dict(r) for r in warnings],
        })
    except Exception:
        return jsonify({"recommendations": [], "iv_warnings": []})
    finally:
        conn.close()


@app.route("/api/signals")
def api_signals():
    conn = _db()
    try:
        rows = conn.execute("""
            SELECT ticker, signal, generated_at, bullish_weight, bearish_weight, total_weight
            FROM signals
            ORDER BY generated_at DESC
            LIMIT 40
        """).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])
    finally:
        conn.close()


@app.route("/api/prices")
def api_prices():
    global _price_cache, _price_cache_ts

    now = time.time()
    with _price_lock:
        if now - _price_cache_ts < 25 and _price_cache:
            return jsonify(list(_price_cache))

    conn = _db()
    try:
        rows = conn.execute(
            "SELECT ticker, price_at_signal FROM performance ORDER BY signal_timestamp DESC"
        ).fetchall()
    except Exception:
        return jsonify([])
    finally:
        conn.close()

    entry_prices: dict[str, float] = {}
    for r in rows:
        t, p = r["ticker"], r["price_at_signal"]
        if t not in entry_prices and p is not None:
            entry_prices[t] = float(p)

    if not entry_prices:
        return jsonify([])

    tickers = list(entry_prices.keys())
    prices: dict[str, float | None] = {}

    with ThreadPoolExecutor(max_workers=min(len(tickers), 8)) as ex:
        future_map = {ex.submit(_fetch_price, t): t for t in tickers}
        try:
            for f in as_completed(future_map, timeout=1.8):
                try:
                    t, price = f.result()
                    prices[t] = price
                except Exception:
                    pass
        except Exception:
            pass

    result = []
    for t in tickers:
        curr = prices.get(t)
        entry = entry_prices.get(t)
        if curr is not None:
            pct = round((curr - entry) / entry * 100, 2) if entry else None
            result.append({
                "ticker": t,
                "price": round(curr, 2),
                "change_pct": pct,
                "entry_price": entry,
            })

    with _price_lock:
        _price_cache = result
        _price_cache_ts = time.time()

    return jsonify(result)


# ── Server ─────────────────────────────────────────────────────────────────────

def start() -> None:
    """Launch Flask in a daemon thread on port 5000."""
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)

    thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False),
        daemon=True,
        name="wsb-dashboard",
    )
    thread.start()
    print("\n  Dashboard  →  http://localhost:5000\n")


# ── HTML ───────────────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WSB Bot</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

:root{
  --bg:#000000;
  --bg2:#1C1C1E;
  --bg3:#2C2C2E;
  --green:#30D158;
  --red:#FF453A;
  --blue:#0A84FF;
  --yellow:#FFD60A;
  --orange:#FF9F0A;
  --text:#FFFFFF;
  --text2:rgba(255,255,255,0.6);
  --text3:rgba(255,255,255,0.28);
  --border:rgba(255,255,255,0.1);
  --glass:rgba(255,255,255,0.05);
  --shadow:0 4px 24px rgba(0,0,0,0.5);
  --r:16px;
  --rs:10px;
}

html{
  font-size:16px;
  -webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;
}

body{
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Helvetica Neue',Arial,sans-serif;
  background:var(--bg);
  color:var(--text);
  min-height:100vh;
}

/* ── Progress bar ─────────────────────────────────────────────────────────── */
.progress-track{
  position:fixed;
  top:0;left:0;right:0;
  height:2px;
  background:rgba(255,255,255,0.06);
  z-index:2000;
  overflow:hidden;
}
.progress-fill{
  height:100%;
  width:100%;
  background:linear-gradient(90deg,var(--blue),rgba(10,132,255,0.4));
  transform-origin:left center;
}
.progress-fill.run{
  animation:prog 30s linear forwards;
}
@keyframes prog{
  from{width:100%}
  to{width:0%}
}

/* ── Nav ──────────────────────────────────────────────────────────────────── */
.nav{
  position:fixed;
  top:2px;left:0;right:0;
  height:52px;
  background:rgba(0,0,0,0.75);
  backdrop-filter:blur(24px) saturate(180%);
  -webkit-backdrop-filter:blur(24px) saturate(180%);
  border-bottom:1px solid var(--border);
  display:flex;
  align-items:center;
  justify-content:space-between;
  padding:0 28px;
  z-index:1000;
}
.nav-left{display:flex;align-items:center;gap:0}
.nav-right{display:flex;align-items:center;gap:20px}
.nav-logo{
  font-size:15px;
  font-weight:700;
  letter-spacing:-0.4px;
  color:var(--text);
}
.nav-logo span{color:var(--blue)}
.nav-sep{
  width:1px;height:18px;
  background:var(--border);
  margin:0 16px;
}
.status-pill{
  display:inline-flex;
  align-items:center;
  gap:7px;
  background:rgba(48,209,88,0.1);
  border:1px solid rgba(48,209,88,0.22);
  border-radius:100px;
  padding:4px 11px;
  font-size:12px;
  font-weight:500;
  color:var(--green);
  transition:all .3s ease;
}
.status-pill.offline{
  background:rgba(255,69,58,0.1);
  border-color:rgba(255,69,58,0.22);
  color:var(--red);
}
.status-dot{
  width:6px;height:6px;
  background:var(--green);
  border-radius:50%;
  animation:pulse 2.4s ease-in-out infinite;
}
.status-pill.offline .status-dot{
  background:var(--red);
  animation:none;
}
@keyframes pulse{
  0%,100%{opacity:1;transform:scale(1)}
  50%{opacity:.5;transform:scale(.7)}
}
.nav-time{
  font-size:12px;
  color:var(--text3);
  font-variant-numeric:tabular-nums;
  letter-spacing:0.01em;
}

/* ── Prices live indicator ────────────────────────────────────────────────── */
.prices-live{
  display:inline-flex;
  align-items:center;
  gap:6px;
  font-size:11px;
  color:var(--text3);
  letter-spacing:.02em;
  user-select:none;
}
.pl-dot{
  width:6px;height:6px;
  background:var(--green);
  border-radius:50%;
  flex-shrink:0;
  animation:pulse 2.4s ease-in-out infinite;
}
.pl-dot.flash{
  animation:pl-flash .6s ease-out forwards;
}
@keyframes pl-flash{
  0%{opacity:1;transform:scale(2.2);background:#ffffff}
  60%{transform:scale(1.4);background:var(--green)}
  100%{opacity:1;transform:scale(1);background:var(--green)}
}

/* ── Ticker bar ───────────────────────────────────────────────────────────── */
.ticker-bar{
  position:fixed;
  top:54px;left:0;right:0;
  height:30px;
  background:rgba(0,0,0,0.9);
  backdrop-filter:blur(12px);
  -webkit-backdrop-filter:blur(12px);
  border-bottom:1px solid var(--border);
  overflow:hidden;
  z-index:999;
  display:flex;
  align-items:center;
}
.ticker-track{
  display:inline-flex;
  align-items:center;
  white-space:nowrap;
  will-change:transform;
}
@keyframes ticker-scroll{
  from{transform:translateX(0)}
  to{transform:translateX(-50%)}
}
.ti-item{
  display:inline-flex;
  align-items:center;
  gap:4px;
  padding:0 18px;
  font-size:11.5px;
  font-variant-numeric:tabular-nums;
}
.ti-ticker{
  font-weight:700;
  font-size:12px;
  letter-spacing:-.1px;
  color:var(--text);
}
.ti-sep{color:var(--text3);padding:0 4px;font-size:10px}
.ti-arrow{font-size:8px;line-height:1}

/* ── Layout ───────────────────────────────────────────────────────────────── */
.main{
  padding:94px 24px 52px;
  max-width:1440px;
  margin:0 auto;
  display:flex;
  flex-direction:column;
  gap:20px;
}

/* ── Metric cards ─────────────────────────────────────────────────────────── */
.metrics-row{
  display:grid;
  grid-template-columns:repeat(4,1fr);
  gap:14px;
}
.metric-card{
  background:var(--glass);
  backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);
  border:1px solid var(--border);
  border-radius:var(--r);
  box-shadow:var(--shadow);
  padding:22px 24px 20px;
  position:relative;
  overflow:hidden;
  transition:transform .2s ease,box-shadow .2s ease;
  cursor:default;
}
.metric-card:hover{
  transform:translateY(-2px);
  box-shadow:0 8px 32px rgba(0,0,0,0.6);
}
.metric-card::after{
  content:'';
  position:absolute;
  top:0;left:0;right:0;
  height:2px;
  background:var(--blue);
  border-radius:var(--r) var(--r) 0 0;
}
.metric-card.c-green::after{background:var(--green)}
.metric-card.c-red::after{background:var(--red)}
.metric-card.c-yellow::after{background:var(--yellow)}

.metric-label{
  font-size:10.5px;
  font-weight:600;
  letter-spacing:.09em;
  text-transform:uppercase;
  color:var(--text3);
  margin-bottom:10px;
}
.metric-value{
  font-size:38px;
  font-weight:200;
  letter-spacing:-1.5px;
  color:var(--text);
  line-height:1;
  font-variant-numeric:tabular-nums;
}
.metric-card.c-green .metric-value{color:var(--green)}
.metric-card.c-red .metric-value{color:var(--red)}
.metric-sub{
  font-size:11px;
  color:var(--text3);
  margin-top:7px;
  letter-spacing:.01em;
}

/* ── Card shell ───────────────────────────────────────────────────────────── */
.card{
  background:var(--glass);
  backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);
  border:1px solid var(--border);
  border-radius:var(--r);
  box-shadow:var(--shadow);
  padding:24px;
}
.card-head{
  display:flex;
  align-items:center;
  justify-content:space-between;
  margin-bottom:18px;
}
.card-title{
  font-size:15px;
  font-weight:600;
  letter-spacing:-.2px;
}
.card-badge{
  font-size:11px;
  color:var(--text3);
  background:rgba(255,255,255,0.06);
  border:1px solid var(--border);
  border-radius:100px;
  padding:3px 9px;
}

/* ── Performance table ────────────────────────────────────────────────────── */
.table-scroll{
  overflow-x:auto;
  margin:0 -24px;
  padding:0 24px;
}
.perf-table{
  width:100%;
  border-collapse:collapse;
  font-size:12.5px;
  min-width:820px;
}
.perf-table thead th{
  font-size:10.5px;
  font-weight:600;
  letter-spacing:.07em;
  text-transform:uppercase;
  color:var(--text3);
  padding:6px 14px 10px;
  text-align:left;
  border-bottom:1px solid rgba(255,255,255,0.07);
  white-space:nowrap;
}
.perf-table thead th:first-child{padding-left:20px}
.perf-table tbody tr{
  transition:background .12s ease;
  cursor:default;
  animation:rowIn .25s ease both;
}
@keyframes rowIn{
  from{opacity:0;transform:translateY(5px)}
  to{opacity:1;transform:translateY(0)}
}
.perf-table tbody tr:hover td{background:rgba(255,255,255,0.03)}
.perf-table tbody td{
  padding:9px 14px;
  border-bottom:1px solid rgba(255,255,255,0.04);
  vertical-align:middle;
  white-space:nowrap;
  color:var(--text);
}
.perf-table tbody td:first-child{padding-left:18px}
.perf-table tbody tr.t-win td:first-child{box-shadow:inset 3px 0 0 var(--green)}
.perf-table tbody tr.t-loss td:first-child{box-shadow:inset 3px 0 0 var(--red)}
.perf-table tbody tr.t-pending td:first-child{box-shadow:inset 3px 0 0 var(--blue)}
.perf-table tbody tr.t-win{background:rgba(48,209,88,.025)}
.perf-table tbody tr.t-loss{background:rgba(255,69,58,.025)}
.perf-table tbody tr.t-pending{background:rgba(10,132,255,.02)}

.ticker-name{
  font-size:13.5px;
  font-weight:600;
  letter-spacing:-.2px;
}
.mono{
  font-variant-numeric:tabular-nums;
  font-size:12px;
}

/* ── Badges ───────────────────────────────────────────────────────────────── */
.b{
  display:inline-flex;
  align-items:center;
  padding:2px 8px;
  border-radius:100px;
  font-size:10.5px;
  font-weight:600;
  letter-spacing:.04em;
  white-space:nowrap;
}
.b-buy{background:rgba(48,209,88,.14);color:var(--green)}
.b-sell{background:rgba(255,69,58,.14);color:var(--red)}
.b-hold{background:rgba(10,132,255,.14);color:var(--blue)}
.b-win{background:rgba(48,209,88,.14);color:var(--green)}
.b-loss{background:rgba(255,69,58,.14);color:var(--red)}
.b-pending{background:rgba(10,132,255,.14);color:var(--blue)}
.b-warn{background:rgba(255,214,10,.14);color:var(--yellow)}

.up{color:var(--green)}
.dn{color:var(--red)}
.neu{color:var(--text3)}

/* ── Options grid ─────────────────────────────────────────────────────────── */
.options-grid{
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:14px;
}
.option-item{
  background:rgba(255,255,255,0.03);
  border:1px solid var(--border);
  border-radius:var(--rs);
  padding:14px 16px;
  margin-bottom:10px;
  transition:background .15s;
}
.option-item:last-child{margin-bottom:0}
.option-item:hover{background:rgba(255,255,255,0.055)}
.option-item-head{
  display:flex;
  align-items:center;
  justify-content:space-between;
  margin-bottom:9px;
}
.option-ticker{
  font-size:15px;
  font-weight:600;
  letter-spacing:-.3px;
}
.option-meta{
  display:flex;
  flex-wrap:wrap;
  gap:14px;
}
.ometa{font-size:11px}
.ometa-l{
  display:block;
  font-size:9.5px;
  font-weight:600;
  letter-spacing:.07em;
  text-transform:uppercase;
  color:var(--text3);
  margin-bottom:1px;
}
.ometa-v{
  color:var(--text2);
  font-variant-numeric:tabular-nums;
}
.iv-item{
  background:rgba(255,69,58,0.07);
  border:1px solid rgba(255,69,58,0.18);
  border-radius:var(--rs);
  padding:14px 16px;
  margin-bottom:10px;
}
.iv-item.iv-warn{
  background:rgba(255,214,10,0.07);
  border-color:rgba(255,214,10,0.18);
}
.iv-item:last-child{margin-bottom:0}

/* ── Signal feed ──────────────────────────────────────────────────────────── */
.signal-wrap{
  display:flex;
  flex-wrap:wrap;
  gap:8px;
  min-height:44px;
  align-content:flex-start;
}
.sig-pill{
  display:inline-flex;
  align-items:center;
  gap:5px;
  padding:5px 12px;
  border-radius:100px;
  font-size:12px;
  font-weight:500;
  animation:rowIn .25s ease both;
  white-space:nowrap;
}
.sig-pill.sp-buy{background:rgba(48,209,88,.1);border:1px solid rgba(48,209,88,.2);color:var(--green)}
.sig-pill.sp-sell{background:rgba(255,69,58,.1);border:1px solid rgba(255,69,58,.2);color:var(--red)}
.sig-pill.sp-hold{background:rgba(10,132,255,.1);border:1px solid rgba(10,132,255,.2);color:var(--blue)}
.sig-time{color:rgba(255,255,255,0.35);font-size:10.5px;font-variant-numeric:tabular-nums}
.sig-ticker{font-weight:700}

/* ── Empty states ─────────────────────────────────────────────────────────── */
.empty{color:var(--text3);font-size:13px;padding:28px 0;text-align:center}
.empty-cell{color:var(--text3);font-size:13px;padding:40px 24px !important;text-align:center}

/* ── Responsive ───────────────────────────────────────────────────────────── */
@media(max-width:960px){
  .metrics-row{grid-template-columns:repeat(2,1fr)}
  .options-grid{grid-template-columns:1fr}
}
@media(max-width:540px){
  .metrics-row{grid-template-columns:1fr}
  .main{padding:90px 16px 40px}
  .nav{padding:0 16px}
}
</style>
</head>
<body>

<!-- Progress bar -->
<div class="progress-track"><div class="progress-fill" id="pgFill"></div></div>

<!-- Nav -->
<nav class="nav">
  <div class="nav-left">
    <span class="nav-logo">WSB<span>&nbsp;Bot</span></span>
    <div class="nav-sep"></div>
    <div class="status-pill" id="statusPill">
      <div class="status-dot"></div>
      <span id="statusLabel">Loading</span>
    </div>
  </div>
  <div class="nav-right">
    <div class="prices-live">
      <div class="pl-dot" id="plDot"></div>
      <span>Prices live</span>
    </div>
    <span class="nav-time" id="navTime">—</span>
  </div>
</nav>

<!-- Live ticker bar -->
<div class="ticker-bar">
  <div class="ticker-track" id="tickerTrack">
    <span class="ti-item"><span class="ti-ticker" style="color:var(--text3);font-weight:400">Fetching prices…</span></span>
  </div>
</div>

<!-- Main -->
<main class="main">

  <!-- Metric cards -->
  <div class="metrics-row">
    <div class="metric-card c-blue" id="mc0">
      <div class="metric-label">Total Signals</div>
      <div class="metric-value" id="mv0">—</div>
      <div class="metric-sub">tracked entries</div>
    </div>
    <div class="metric-card c-blue" id="mc1">
      <div class="metric-label">6h Win Rate</div>
      <div class="metric-value" id="mv1">—</div>
      <div class="metric-sub" id="ms1">resolved signals</div>
    </div>
    <div class="metric-card c-blue" id="mc2">
      <div class="metric-label">24h Win Rate</div>
      <div class="metric-value" id="mv2">—</div>
      <div class="metric-sub" id="ms2">resolved signals</div>
    </div>
    <div class="metric-card c-blue" id="mc3">
      <div class="metric-label">Best Call</div>
      <div class="metric-value" id="mv3">—</div>
      <div class="metric-sub" id="ms3">top performer</div>
    </div>
  </div>

  <!-- Performance table -->
  <div class="card">
    <div class="card-head">
      <span class="card-title">Performance</span>
      <span class="card-badge" id="perfBadge">Loading…</span>
    </div>
    <div class="table-scroll">
      <table class="perf-table">
        <thead>
          <tr>
            <th>Ticker</th>
            <th>Direction</th>
            <th>Entry</th>
            <th>Live</th>
            <th>Chg</th>
            <th>6h</th>
            <th>24h</th>
            <th>7d</th>
            <th>Option</th>
            <th>IV</th>
            <th>Outcome</th>
          </tr>
        </thead>
        <tbody id="perfBody">
          <tr><td colspan="11" class="empty-cell">Loading performance data…</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Options panels -->
  <div class="options-grid">
    <div class="card">
      <div class="card-head">
        <span class="card-title">Active Recommendations</span>
        <span class="card-badge" id="recsBadge">—</span>
      </div>
      <div id="optRecs"><p class="empty">Loading…</p></div>
    </div>
    <div class="card">
      <div class="card-head">
        <span class="card-title">IV Warnings</span>
        <span class="card-badge" style="color:var(--yellow)">High volatility</span>
      </div>
      <div id="ivWarn"><p class="empty">Loading…</p></div>
    </div>
  </div>

  <!-- Signal feed -->
  <div class="card">
    <div class="card-head">
      <span class="card-title">Signal Feed</span>
      <span class="card-badge" id="sigBadge">Recent</span>
    </div>
    <div class="signal-wrap" id="sigFeed">
      <p class="empty">Loading signals…</p>
    </div>
  </div>

</main>

<script>
const PIPELINE_INTERVAL = 960000; // 16 minutes
const PRICE_INTERVAL    = 30000;  // 30 seconds

async function get(url) {
  try { const r = await fetch(url); return r.ok ? r.json() : null; }
  catch { return null; }
}

function fmt$(v) {
  return v == null ? '<span class="neu">—</span>' : '$' + Number(v).toFixed(2);
}
function fmtPct(v) {
  if (v == null) return '<span class="neu">—</span>';
  const cls = v > 0 ? 'up' : v < 0 ? 'dn' : 'neu';
  return `<span class="${cls}">${v > 0 ? '+' : ''}${Number(v).toFixed(2)}%</span>`;
}
function fmtIV(v) {
  if (v == null) return '<span class="neu">—</span>';
  const pct = v * 100;
  const cls = pct > 80 ? 'dn' : pct > 50 ? '' : 'up';
  return `<span class="${cls}">${pct.toFixed(0)}%</span>`;
}
function shortTime(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts.includes('T') ? ts : ts.replace(' ', 'T') + 'Z');
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch { return ts.slice(11, 16) || '—'; }
}
function timeAgo(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts.includes('T') ? ts : ts.replace(' ', 'T') + 'Z');
    const s = Math.floor((Date.now() - d) / 1000);
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
  } catch { return '—'; }
}

async function refreshStats() {
  const d = await get('/api/stats');
  if (!d) return;

  const pill = document.getElementById('statusPill');
  const lbl  = document.getElementById('statusLabel');
  if (d.bot_running) {
    pill.classList.remove('offline');
    lbl.textContent = 'Live';
  } else {
    pill.classList.add('offline');
    lbl.textContent = 'Idle';
  }
  document.getElementById('navTime').textContent = d.last_updated || '—';

  document.getElementById('mv0').textContent = d.total_signals ?? '—';

  const wr6 = d.win_rate_6h;
  const mc1 = document.getElementById('mc1');
  const mv1 = document.getElementById('mv1');
  if (wr6 != null) {
    mv1.textContent = wr6 + '%';
    mc1.className = 'metric-card ' + (wr6 >= 50 ? 'c-green' : 'c-red');
  } else {
    mv1.textContent = 'N/A';
    mc1.className = 'metric-card c-blue';
  }

  const wr24 = d.win_rate_24h;
  const mc2  = document.getElementById('mc2');
  const mv2  = document.getElementById('mv2');
  if (wr24 != null) {
    mv2.textContent = wr24 + '%';
    mc2.className = 'metric-card ' + (wr24 >= 50 ? 'c-green' : 'c-red');
  } else {
    mv2.textContent = 'N/A';
    mc2.className = 'metric-card c-blue';
  }

  const mc3 = document.getElementById('mc3');
  const mv3 = document.getElementById('mv3');
  const ms3 = document.getElementById('ms3');
  if (d.best_call) {
    const sign = d.best_call.pct > 0 ? '+' : '';
    mv3.textContent = d.best_call.ticker;
    ms3.textContent = sign + d.best_call.pct + '%  ' + d.best_call.signal;
    mc3.className = 'metric-card c-green';
  } else {
    mv3.textContent = 'N/A';
    ms3.textContent = 'no resolved signals';
    mc3.className = 'metric-card c-blue';
  }
}

async function refreshPerf() {
  const rows = await get('/api/performance');
  if (!rows) return;

  document.getElementById('perfBadge').textContent = rows.length + ' record' + (rows.length !== 1 ? 's' : '');

  const tbody = document.getElementById('perfBody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="11" class="empty-cell">No performance data yet — run the bot to generate signals.</td></tr>';
    return;
  }

  tbody.innerHTML = rows.map((r, i) => {
    const out24 = r.outcome || 'PENDING';
    const out6  = r.outcome_6h || 'PENDING';
    const eff   = out24 !== 'PENDING' ? out24 : out6;
    const trCls = eff === 'WIN' ? 't-win' : eff === 'LOSS' ? 't-loss' : 't-pending';

    const dirBadge = r.signal === 'BUY'
      ? '<span class="b b-buy">BUY</span>'
      : r.signal === 'SELL'
      ? '<span class="b b-sell">SELL</span>'
      : '<span class="b b-hold">HOLD</span>';

    const outBadge = eff === 'WIN'
      ? '<span class="b b-win">WIN</span>'
      : eff === 'LOSS'
      ? '<span class="b b-loss">LOSS</span>'
      : '<span class="b b-pending">PENDING</span>';

    const opt = r.option_contract
      ? `<span title="${r.option_contract}">${r.option_type || '?'}&nbsp;$${r.option_strike || '?'}</span>`
      : '<span class="neu">—</span>';

    const tk = (r.ticker || '').replace(/[^A-Z0-9.]/g, '');

    return `<tr class="${trCls}" style="animation-delay:${Math.min(i * 15, 300)}ms">
      <td><span class="ticker-name">${r.ticker}</span></td>
      <td>${dirBadge}</td>
      <td class="mono">${fmt$(r.price_at_signal)}</td>
      <td class="mono price-cell" data-ticker="${tk}"><span class="neu">—</span></td>
      <td class="mono chg-cell"   data-ticker="${tk}"><span class="neu">—</span></td>
      <td class="mono">${fmtPct(r.percent_change_6h)}</td>
      <td class="mono">${fmtPct(r.percent_change_24h)}</td>
      <td class="mono">${fmtPct(r.percent_change_7d)}</td>
      <td class="mono">${opt}</td>
      <td class="mono">${fmtIV(r.option_iv)}</td>
      <td>${outBadge}</td>
    </tr>`;
  }).join('');
}

async function refreshOptions() {
  const data = await get('/api/options');
  if (!data) return;

  const recs  = data.recommendations || [];
  const warns = data.iv_warnings || [];

  document.getElementById('recsBadge').textContent = recs.length + ' active';

  const recsEl = document.getElementById('optRecs');
  if (!recs.length) {
    recsEl.innerHTML = '<p class="empty">No option recommendations yet</p>';
  } else {
    recsEl.innerHTML = recs.map(r => {
      const badge  = r.signal === 'BUY' ? '<span class="b b-buy">BUY</span>' : '<span class="b b-sell">SELL</span>';
      const strike = r.option_strike ? '$' + Number(r.option_strike).toFixed(0) : '—';
      const cost   = r.option_cost   ? '$' + Number(r.option_cost).toFixed(2)   : '—';
      const iv     = r.option_iv     ? (r.option_iv * 100).toFixed(0) + '%'     : '—';
      const delta  = r.option_delta  ? Number(r.option_delta).toFixed(2)        : '—';
      const when   = timeAgo(r.signal_timestamp);
      return `<div class="option-item">
        <div class="option-item-head">
          <span class="option-ticker">${r.ticker}</span>
          <div style="display:flex;gap:6px;align-items:center">${badge}<span class="neu" style="font-size:11px">${when}</span></div>
        </div>
        <div class="option-meta">
          <div class="ometa"><span class="ometa-l">Type</span><span class="ometa-v">${r.option_type || '—'}</span></div>
          <div class="ometa"><span class="ometa-l">Strike</span><span class="ometa-v">${strike}</span></div>
          <div class="ometa"><span class="ometa-l">Expiry</span><span class="ometa-v">${r.option_expiry || '—'}</span></div>
          <div class="ometa"><span class="ometa-l">Cost</span><span class="ometa-v">${cost}</span></div>
          <div class="ometa"><span class="ometa-l">IV</span><span class="ometa-v ${r.option_iv > 0.8 ? 'dn' : ''}">${iv}</span></div>
          <div class="ometa"><span class="ometa-l">Delta</span><span class="ometa-v">${delta}</span></div>
        </div>
      </div>`;
    }).join('');
  }

  const warnEl = document.getElementById('ivWarn');
  if (!warns.length) {
    warnEl.innerHTML = '<p class="empty">No high-IV warnings</p>';
  } else {
    warnEl.innerHTML = warns.map(r => {
      const ivPct = r.option_iv * 100;
      const cls   = ivPct > 100 ? '' : 'iv-warn';
      const badge = `<span class="b b-warn">IV ${ivPct.toFixed(0)}%</span>`;
      return `<div class="iv-item ${cls}">
        <div class="option-item-head">
          <span class="option-ticker">${r.ticker}</span>
          ${badge}
        </div>
        <div class="option-meta">
          <div class="ometa"><span class="ometa-l">Strike</span><span class="ometa-v">${r.option_strike ? '$' + r.option_strike : '—'}</span></div>
          <div class="ometa"><span class="ometa-l">Expiry</span><span class="ometa-v">${r.option_expiry || '—'}</span></div>
          <div class="ometa"><span class="ometa-l">Signal</span><span class="ometa-v">${r.signal}</span></div>
          <div class="ometa"><span class="ometa-l">Entry</span><span class="ometa-v">${fmt$(r.price_at_signal)}</span></div>
        </div>
      </div>`;
    }).join('');
  }
}

async function refreshSignals() {
  const sigs = await get('/api/signals');
  if (!sigs) return;

  const feed = document.getElementById('sigFeed');
  document.getElementById('sigBadge').textContent = sigs.length + ' recent';

  if (!sigs.length) {
    feed.innerHTML = '<p class="empty">No signals generated yet</p>';
    return;
  }

  feed.innerHTML = sigs.map(s => {
    const cls = s.signal === 'BUY' ? 'sp-buy' : s.signal === 'SELL' ? 'sp-sell' : 'sp-hold';
    const t   = shortTime(s.generated_at);
    return `<div class="sig-pill ${cls}">
      <span class="sig-time">${t}</span>
      <span class="sig-ticker">${s.ticker}</span>
      <span>${s.signal}</span>
    </div>`;
  }).join('');
}

function updateTickerBar(data) {
  if (!data || !data.length) return;
  const track = document.getElementById('tickerTrack');

  const items = data.map(({ ticker, price, change_pct }) => {
    const isUp  = change_pct != null && change_pct >= 0;
    const isDn  = change_pct != null && change_pct < 0;
    const cls   = isDn ? 'dn' : isUp ? 'up' : 'neu';
    const arrow = isDn ? '▼' : isUp ? '▲' : '';
    const pStr  = price != null ? '$' + Number(price).toFixed(2) : '—';
    const cStr  = change_pct != null
      ? (change_pct >= 0 ? '+' : '') + Number(change_pct).toFixed(2) + '%'
      : '';
    return `<span class="ti-item">` +
      `<span class="ti-ticker">${ticker}</span>` +
      ` <span class="${cls}">` +
        (arrow ? `<span class="ti-arrow">${arrow}</span> ` : '') +
        `${pStr}</span>` +
      (cStr ? ` <span class="${cls}">${cStr}</span>` : '') +
      `</span>`;
  }).join('<span class="ti-sep">·</span>');

  // Duplicate for seamless infinite scroll
  const sep  = '<span class="ti-sep" style="padding:0 12px">·</span>';
  track.innerHTML = items + sep + items;

  const dur = Math.max(20, data.length * 5);
  track.style.animation = 'none';
  void track.offsetWidth; // force reflow to restart
  track.style.animation = `ticker-scroll ${dur}s linear infinite`;
}

async function refreshPrices() {
  const data = await get('/api/prices');

  if (data && data.length) {
    data.forEach(({ ticker, price, change_pct }) => {
      const isUp  = change_pct != null && change_pct >= 0;
      const isDn  = change_pct != null && change_pct < 0;
      const cls   = isDn ? 'dn' : isUp ? 'up' : 'neu';
      const arrow = isDn ? '▼' : isUp ? '▲' : '';
      const pStr  = price != null ? '$' + Number(price).toFixed(2) : '—';
      const cStr  = change_pct != null
        ? `<span class="${cls}">${change_pct >= 0 ? '+' : ''}${Number(change_pct).toFixed(2)}%</span>`
        : `<span class="neu">—</span>`;

      const priceHtml = price != null
        ? `<span class="${cls}">${arrow ? `<span class="ti-arrow">${arrow}</span> ` : ''}${pStr}</span>`
        : `<span class="neu">—</span>`;

      document.querySelectorAll(`.price-cell[data-ticker="${ticker}"]`).forEach(el => {
        el.innerHTML = priceHtml;
      });
      document.querySelectorAll(`.chg-cell[data-ticker="${ticker}"]`).forEach(el => {
        el.innerHTML = cStr;
      });
    });

    updateTickerBar(data);
  }

  // Flash the prices-live dot
  const dot = document.getElementById('plDot');
  dot.classList.remove('flash');
  void dot.offsetWidth;
  dot.classList.add('flash');
  setTimeout(() => dot.classList.remove('flash'), 700);

  kickProgress();
}

function kickProgress() {
  const el = document.getElementById('pgFill');
  el.classList.remove('run');
  void el.offsetWidth;
  el.classList.add('run');
}

async function refreshPipeline() {
  await Promise.all([refreshStats(), refreshPerf(), refreshOptions(), refreshSignals()]);
  await refreshPrices(); // fill live price cells immediately after table rebuild
}

refreshPipeline();
setInterval(refreshPipeline, PIPELINE_INTERVAL);
setInterval(refreshPrices,  PRICE_INTERVAL);
</script>
</body>
</html>"""
