"""
WSB Bot web dashboard — Apple dark theme, two-page SPA on port 5000.
Page 1: Today's Signals  (last 24h, actionable)
Page 2: Performance History  (all signals, grouped by date, filterable)
"""

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import yfinance as yf
from flask import Flask, jsonify, Response

from config import DB_PATH, SCHEDULE_INTERVAL_MINUTES

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


def _market_status() -> dict:
    now = datetime.now(timezone.utc)
    et_offset = -4 if 3 <= now.month <= 11 else -5
    et_minutes = (now.hour * 60 + now.minute) + et_offset * 60
    if et_minutes < 0:
        et_minutes += 24 * 60
    et_hour = et_minutes // 60
    et_min  = et_minutes % 60
    weekday = now.weekday()
    if (now.hour * 60 + now.minute) + et_offset * 60 < 0:
        weekday = (weekday - 1) % 7
    is_weekend = weekday >= 5
    is_open = (
        not is_weekend
        and (et_hour > 9 or (et_hour == 9 and et_min >= 30))
        and et_hour < 16
    )
    is_after_hours = (not is_weekend) and (not is_open)
    next_open = None
    if not is_open:
        if is_weekend:
            next_open = "Monday 9:30am ET"
        elif et_hour >= 16:
            next_open = "Monday 9:30am ET" if weekday == 4 else "Tomorrow 9:30am ET"
        else:
            next_open = "Today 9:30am ET"
    return {
        "open": is_open,
        "next_open": next_open,
        "is_weekend": is_weekend,
        "is_after_hours": is_after_hours,
    }


# ── API ────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")


@app.route("/api/today")
def api_today():
    conn = _db()
    try:
        rows = conn.execute("""
            SELECT p.*,
                   s.bullish_weight, s.bearish_weight, s.total_weight
            FROM performance p
            LEFT JOIN signals s
              ON  s.ticker      = p.ticker
              AND s.signal      = p.signal
              AND s.generated_at = p.signal_timestamp
            WHERE p.signal_timestamp >= datetime('now', '-24 hours')
              AND p.signal IN ('BUY', 'SELL')
            ORDER BY p.signal_timestamp DESC
        """).fetchall()

        # Count signals per ticker, then dedupe keeping highest total_weight
        counts: dict[str, int] = {}
        best: dict[str, dict] = {}
        for r in rows:
            d = dict(r)
            t  = d["ticker"]
            tw = float(d.get("total_weight") or 0)
            counts[t] = counts.get(t, 0) + 1
            if t not in best or tw > float(best[t].get("total_weight") or 0):
                best[t] = d

        result = []
        for ticker, d in best.items():
            d["signal_count"] = counts[ticker]
            bw = float(d.get("bullish_weight") or 0)
            sw = float(d.get("bearish_weight") or 0)
            tw = float(d.get("total_weight") or 0)
            if tw > 0:
                d["confidence"] = round(bw / tw if d["signal"] == "BUY" else sw / tw, 3)
            else:
                d["confidence"] = None
            result.append(d)

        result.sort(key=lambda x: float(x.get("total_weight") or 0), reverse=True)

        ms = _market_status()
        return jsonify({
            "signals": result,
            "market_open": ms["open"],
            "next_open": ms["next_open"],
            "is_weekend": ms["is_weekend"],
            "is_after_hours": ms["is_after_hours"],
        })
    except Exception as e:
        return jsonify({"signals": [], "market_open": False, "next_open": None,
                        "is_weekend": False, "is_after_hours": False, "error": str(e)})
    finally:
        conn.close()


@app.route("/api/history")
def api_history():
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM performance WHERE signal IN ('BUY','SELL') ORDER BY signal_timestamp DESC LIMIT 500"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])
    finally:
        conn.close()


@app.route("/api/stats")
def api_stats():
    rows: list[dict] = []
    bot_status_row: dict | None = None
    try:
        conn = _db()
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM performance").fetchall()]
            try:
                bot_row = conn.execute("SELECT * FROM bot_status WHERE id = 1").fetchone()
                bot_status_row = dict(bot_row) if bot_row else None
            except Exception:
                pass
        except Exception:
            pass
        finally:
            conn.close()
    except Exception:
        pass

    try:
        # Exclude HOLD rows from all stats
        rows = [r for r in rows if r.get("signal") in ("BUY", "SELL")]
        total = len(rows)

        # ── Stock win rates (only resolved checkpoints) ───────────────────────
        resolved_6h  = [r for r in rows if r.get("percent_change_6h") is not None]
        wins_6h      = sum(1 for r in resolved_6h if r.get("outcome_6h") == "WIN")
        win_rate_6h  = round(wins_6h / len(resolved_6h) * 100, 1) if resolved_6h else None
        resolved_24h = [r for r in rows if r.get("percent_change_24h") is not None]
        wins_24h     = sum(1 for r in resolved_24h if r.get("outcome") == "WIN")
        win_rate_24h = round(wins_24h / len(resolved_24h) * 100, 1) if resolved_24h else None

        def _pct(r):
            return r.get("percent_change_24h") or r.get("percent_change_6h") or 0.0

        resolved_any = [r for r in rows if r.get("percent_change_24h") is not None or r.get("percent_change_6h") is not None]
        best_call = None
        if resolved_any:
            best = max(resolved_any, key=_pct)
            pct  = _pct(best)
            if pct > 0:
                best_call = {"ticker": best["ticker"], "pct": round(pct, 2), "signal": best["signal"]}

        def _opt_return(r):
            for col in ("option_return_7d", "option_return_24h", "option_return_6h",
                        "close_return_pct"):
                v = r.get(col)
                if v is not None:
                    return v
            return 0.0

        def _opt_profit(r):
            cost = r.get("option_cost")
            for profit_col in ("close_profit_dollars", "option_profit_7d", "option_profit_24h", "option_profit_6h"):
                stored = r.get(profit_col)
                if stored is not None:
                    return stored
            for val_col in ("close_option_value", "option_value_7d", "option_value_24h", "option_value_6h"):
                val = r.get(val_col)
                if val is not None and cost is not None:
                    return val - cost
            return None

        # Options win rate: trades with any resolved option return
        resolved_opts = [r for r in rows if (
            any(r.get(c) is not None for c in ("option_return_6h","option_return_24h","option_return_7d"))
            or r.get("close_return_pct") is not None
        )]
        best_option_return = None
        if resolved_opts:
            best_opt = max(resolved_opts, key=_opt_return)
            opt_pct  = _opt_return(best_opt)
            best_option_return = {"ticker": best_opt["ticker"], "pct": round(opt_pct, 2), "signal": best_opt["signal"]}

        avg_option_return = (round(sum(_opt_return(r) for r in resolved_opts) / len(resolved_opts), 1) if resolved_opts else None)
        opt_wins      = sum(1 for r in resolved_opts if _opt_return(r) > 0)
        option_win_rate = round(opt_wins / len(resolved_opts) * 100, 1) if resolved_opts else None
        buy_opts  = [r for r in resolved_opts if r.get("signal") == "BUY"]
        sell_opts = [r for r in resolved_opts if r.get("signal") == "SELL"]

        def _sum_profit(group):
            return round(sum(p for r in group for p in [_opt_profit(r)] if p is not None), 2)

        total_buy_pnl      = _sum_profit(buy_opts)
        total_sell_pnl     = _sum_profit(sell_opts)
        total_combined_pnl = round(total_buy_pnl + total_sell_pnl, 2)

        # ── Closed trades summary ─────────────────────────────────────────────
        closed_rows    = [r for r in rows if (r.get("trade_status") or "").startswith("CLOSED")]
        closed_profit  = [r for r in closed_rows if r.get("trade_status") == "CLOSED_PROFIT"]
        closed_loss    = [r for r in closed_rows if r.get("trade_status") == "CLOSED_LOSS"]
        closed_expiry  = [r for r in closed_rows if r.get("trade_status") == "CLOSED_EXPIRY"]
        closed_rev     = [r for r in closed_rows if r.get("trade_status") == "CLOSED_REVERSAL"]
        active_rows    = [r for r in rows if (r.get("trade_status") or "PENDING") == "ACTIVE"]
        def _avg_ret(group):
            vals = [r.get("close_return_pct") for r in group if r.get("close_return_pct") is not None]
            return round(sum(vals) / len(vals), 1) if vals else None
        closed_summary = {
            "total": len(closed_rows),
            "profit": len(closed_profit), "avg_profit_pct": _avg_ret(closed_profit),
            "loss":   len(closed_loss),   "avg_loss_pct":   _avg_ret(closed_loss),
            "expiry": len(closed_expiry),
            "reversal": len(closed_rev),
            "active": len(active_rows),
        }

        now = datetime.now(timezone.utc)
        bot_state = "offline"
        minutes_since_heartbeat = None
        minutes_until_next_run  = None
        last_updated  = "Never"
        last_run_duration    = None
        last_posts_collected = None
        last_signals_generated = None

        hb_str = None
        if bot_status_row and bot_status_row.get("last_heartbeat"):
            hb_str = bot_status_row["last_heartbeat"]
        elif _last_run:
            hb_str = _last_run.strftime("%Y-%m-%d %H:%M:%S")

        if hb_str:
            try:
                hb = datetime.fromisoformat(hb_str).replace(tzinfo=timezone.utc)
                age_mins = (now - hb).total_seconds() / 60
                minutes_since_heartbeat = round(age_mins, 1)
                minutes_until_next_run  = max(0, round(SCHEDULE_INTERVAL_MINUTES - age_mins, 1))
                last_updated = hb.strftime("%Y-%m-%d %H:%M UTC")
                if age_mins < 20:
                    bot_state = "live"
                elif age_mins < 60:
                    bot_state = "idle"
                if bot_status_row:
                    last_run_duration      = bot_status_row.get("last_run_duration")
                    last_posts_collected   = bot_status_row.get("last_posts_collected")
                    last_signals_generated = bot_status_row.get("last_signals_generated")
            except Exception:
                pass

        return jsonify({
            "total_signals": total,
            "stock_win_rate_6h": win_rate_6h,
            "stock_win_rate_24h": win_rate_24h,
            "win_rate_6h": win_rate_6h,    # kept for back-compat
            "win_rate_24h": win_rate_24h,  # kept for back-compat
            "best_call": best_call,
            "last_updated": last_updated,
            "bot_running": bot_state == "live",
            "bot_state": bot_state,
            "minutes_since_heartbeat": minutes_since_heartbeat,
            "minutes_until_next_run": minutes_until_next_run,
            "last_run_duration": last_run_duration,
            "last_posts_collected": last_posts_collected,
            "last_signals_generated": last_signals_generated,
            "best_option_return": best_option_return,
            "avg_option_return": avg_option_return,
            "option_win_rate": option_win_rate,
            "total_buy_pnl": total_buy_pnl,
            "total_sell_pnl": total_sell_pnl,
            "total_combined_pnl": total_combined_pnl,
            "resolved_options_count": len(resolved_opts),
            "closed_summary": closed_summary,
        })
    except Exception:
        return jsonify({"error": "stats computation failed", "total_signals": 0, "bot_state": "offline", "bot_running": False, "last_updated": "Error"})


@app.route("/api/performance")
def api_performance():
    conn = _db()
    try:
        rows = conn.execute("SELECT * FROM performance ORDER BY signal_timestamp DESC LIMIT 100").fetchall()
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
            SELECT * FROM performance WHERE option_contract IS NOT NULL
            ORDER BY signal_timestamp DESC LIMIT 15
        """).fetchall()
        warnings = conn.execute("""
            SELECT * FROM performance
            WHERE option_iv IS NOT NULL AND option_iv > 0.8 AND outcome = 'PENDING'
            ORDER BY option_iv DESC LIMIT 10
        """).fetchall()
        return jsonify({"recommendations": [dict(r) for r in recs], "iv_warnings": [dict(r) for r in warnings]})
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
            FROM signals ORDER BY generated_at DESC LIMIT 40
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
        rows = conn.execute("SELECT ticker, price_at_signal FROM performance ORDER BY signal_timestamp DESC").fetchall()
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
        try:
            curr  = prices.get(t)
            entry = entry_prices.get(t)
            if curr is not None:
                pct = round((curr - entry) / entry * 100, 2) if entry else None
                result.append({"ticker": t, "price": round(curr, 2), "change_pct": pct, "entry_price": entry})
        except Exception:
            pass

    with _price_lock:
        _price_cache = result
        _price_cache_ts = time.time()

    return jsonify(result)


# ── Server ─────────────────────────────────────────────────────────────────────

def start() -> None:
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
  --bg:#000;--bg2:#1C1C1E;--bg3:#2C2C2E;
  --green:#30D158;--red:#FF453A;--blue:#0A84FF;
  --yellow:#FFD60A;--orange:#FF9F0A;
  --text:#fff;--text2:rgba(255,255,255,.6);--text3:rgba(255,255,255,.28);
  --border:rgba(255,255,255,.1);--glass:rgba(255,255,255,.05);
  --shadow:0 4px 24px rgba(0,0,0,.5);--r:16px;--rs:10px;
}
html{font-size:16px;-webkit-font-smoothing:antialiased}
body{font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Helvetica Neue',Arial,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}

/* progress */
.progress-track{position:fixed;top:0;left:0;right:0;height:2px;background:rgba(255,255,255,.06);z-index:2000;overflow:hidden}
.progress-fill{height:100%;width:100%;background:linear-gradient(90deg,var(--blue),rgba(10,132,255,.4));transform-origin:left center}
.progress-fill.run{animation:prog 30s linear forwards}
@keyframes prog{from{width:100%}to{width:0%}}

/* nav */
.nav{position:fixed;top:2px;left:0;right:0;height:52px;background:rgba(0,0,0,.75);backdrop-filter:blur(24px) saturate(180%);-webkit-backdrop-filter:blur(24px) saturate(180%);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;padding:0 24px;z-index:1000}
.nav-left{display:flex;align-items:center;gap:0}
.nav-right{display:flex;align-items:center;gap:16px}
.nav-logo{font-size:15px;font-weight:700;letter-spacing:-.4px}
.nav-logo span{color:var(--blue)}
.nav-sep{width:1px;height:18px;background:var(--border);margin:0 14px}
.status-pill{display:inline-flex;align-items:center;gap:7px;background:rgba(48,209,88,.1);border:1px solid rgba(48,209,88,.22);border-radius:100px;padding:4px 11px;font-size:12px;font-weight:500;color:var(--green);transition:all .3s}
.status-pill.offline{background:rgba(255,69,58,.1);border-color:rgba(255,69,58,.22);color:var(--red)}
.status-pill.idle{background:rgba(255,214,10,.1);border-color:rgba(255,214,10,.22);color:var(--yellow)}
.status-dot{width:6px;height:6px;background:var(--green);border-radius:50%;animation:pulse 2.4s ease-in-out infinite}
.status-pill.offline .status-dot{background:var(--red);animation:none}
.status-pill.idle .status-dot{background:var(--yellow);animation:none}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(.7)}}
.nav-stat{font-size:11px;color:var(--text3);white-space:nowrap;letter-spacing:.01em}
.prices-live{display:inline-flex;align-items:center;gap:6px;font-size:11px;color:var(--text3);user-select:none}
.pl-dot{width:6px;height:6px;background:var(--green);border-radius:50%;flex-shrink:0;animation:pulse 2.4s ease-in-out infinite}
.pl-dot.flash{animation:pl-flash .6s ease-out forwards}
@keyframes pl-flash{0%{opacity:1;transform:scale(2.2);background:#fff}60%{transform:scale(1.4);background:var(--green)}100%{opacity:1;transform:scale(1);background:var(--green)}}
.nav-time{font-size:12px;color:var(--text3);font-variant-numeric:tabular-nums;letter-spacing:.01em}

/* tab bar */
.tab-bar{position:fixed;top:54px;left:0;right:0;height:44px;background:rgba(0,0,0,.88);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:center;gap:6px;z-index:998;padding:0 24px}
.tab-btn{padding:7px 22px;border-radius:8px;font-size:13px;font-weight:500;color:var(--text2);cursor:pointer;border:none;background:transparent;transition:all .18s ease;letter-spacing:-.1px;white-space:nowrap}
.tab-btn.active{background:var(--blue);color:#fff}
.tab-btn:hover:not(.active){background:rgba(255,255,255,.08);color:var(--text)}

/* ticker bar */
.ticker-bar{position:fixed;top:98px;left:0;right:0;height:30px;background:rgba(0,0,0,.9);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--border);overflow:hidden;z-index:997;display:flex;align-items:center}
.ticker-track{display:inline-flex;align-items:center;white-space:nowrap;will-change:transform}
@keyframes ticker-scroll{from{transform:translateX(0)}to{transform:translateX(-50%)}}
.ti-item{display:inline-flex;align-items:center;gap:4px;padding:0 18px;font-size:11.5px;font-variant-numeric:tabular-nums}
.ti-ticker{font-weight:700;font-size:12px;letter-spacing:-.1px;color:var(--text)}
.ti-sep{color:var(--text3);padding:0 4px;font-size:10px}
.ti-arrow{font-size:8px;line-height:1}

/* layout */
.main{padding:148px 24px 52px;max-width:1440px;margin:0 auto;display:flex;flex-direction:column;gap:18px}

/* shared card */
.card{background:var(--glass);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border:1px solid var(--border);border-radius:var(--r);box-shadow:var(--shadow);padding:22px 24px}
.card-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
.card-title{font-size:15px;font-weight:600;letter-spacing:-.2px}
.card-badge{font-size:11px;color:var(--text3);background:rgba(255,255,255,.06);border:1px solid var(--border);border-radius:100px;padding:3px 9px}

/* badges */
.b{display:inline-flex;align-items:center;padding:2px 8px;border-radius:100px;font-size:10.5px;font-weight:600;letter-spacing:.04em;white-space:nowrap}
.b-buy{background:rgba(48,209,88,.14);color:var(--green)}
.b-sell{background:rgba(255,69,58,.14);color:var(--red)}
.b-hold{background:rgba(10,132,255,.14);color:var(--blue)}
.b-win{background:rgba(48,209,88,.14);color:var(--green)}
.b-loss{background:rgba(255,69,58,.14);color:var(--red)}
.b-pending{background:rgba(10,132,255,.14);color:var(--blue)}
.b-warn{background:rgba(255,214,10,.14);color:var(--yellow)}
.up{color:var(--green)}.dn{color:var(--red)}.neu{color:var(--text3)}
.mono{font-variant-numeric:tabular-nums;font-size:12px}

/* strong signal badge */
.strong-badge{display:inline-flex;align-items:center;gap:3px;padding:2px 8px;border-radius:100px;font-size:10px;font-weight:700;background:rgba(255,214,10,.15);color:var(--yellow);border:1px solid rgba(255,214,10,.3);letter-spacing:.04em;white-space:nowrap}

/* confirming signals badge */
.confirm-badge{display:inline-flex;align-items:center;gap:3px;padding:2px 8px;border-radius:100px;font-size:10px;font-weight:600;background:rgba(10,132,255,.12);color:var(--blue);border:1px solid rgba(10,132,255,.25);white-space:nowrap}

/* signal strength bar */
.strength-bar-wrap{margin-top:10px}
.strength-bar-label{display:flex;justify-content:space-between;font-size:10px;color:var(--text3);margin-bottom:4px}
.strength-track{height:4px;background:rgba(255,255,255,.08);border-radius:100px;overflow:hidden}
.strength-fill{height:100%;border-radius:100px;background:linear-gradient(90deg,var(--blue),var(--green));transition:width .4s ease}

/* trade status badge colors */
.b-closed-profit{background:rgba(48,209,88,.18);color:var(--green);border:1px solid rgba(48,209,88,.3)}
.b-closed-loss{background:rgba(255,69,58,.18);color:var(--red);border:1px solid rgba(255,69,58,.3)}
.b-closed-expiry{background:rgba(142,142,147,.14);color:#8E8E93;border:1px solid rgba(142,142,147,.25)}
.b-closed-reversal{background:rgba(255,214,10,.14);color:var(--yellow);border:1px solid rgba(255,214,10,.25)}
.b-active{background:rgba(10,132,255,.14);color:var(--blue)}

/* closed trades summary */
.closed-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:14px}
.cs-item{background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:var(--rs);padding:12px 14px;text-align:center}
.cs-num{font-size:24px;font-weight:200;letter-spacing:-1px;font-variant-numeric:tabular-nums}
.cs-label{font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--text3);margin-top:2px}
.cs-sub{font-size:10px;color:var(--text3);margin-top:2px}
@media(max-width:600px){.closed-summary{grid-template-columns:repeat(2,1fr)}}

/* ── TODAY PAGE ────────────────────────────────────────────────────────────── */

/* market banner */
.market-banner{display:flex;align-items:center;gap:12px;background:rgba(255,214,10,.07);border:1px solid rgba(255,214,10,.2);border-radius:var(--rs);padding:12px 18px;font-size:13px;color:var(--yellow);font-weight:500}
.market-banner-icon{font-size:16px;flex-shrink:0}
.market-banner-text span{color:rgba(255,214,10,.6);font-weight:400}

/* summary bar */
.today-summary{display:flex;align-items:center;background:var(--glass);border:1px solid var(--border);border-radius:var(--r);padding:18px 24px;gap:0}
.ts-stat{display:flex;align-items:center;gap:12px;flex:1;justify-content:center}
.ts-num{font-size:36px;font-weight:200;letter-spacing:-1.2px;font-variant-numeric:tabular-nums;line-height:1}
.ts-num.ts-buy{color:var(--green)}.ts-num.ts-sell{color:var(--red)}.ts-num.ts-hold{color:var(--blue)}
.ts-label{font-size:11px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--text3)}
.ts-div{width:1px;height:36px;background:var(--border)}

/* signal cards grid */
.today-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:14px}

.sig-card{background:var(--glass);border:1px solid var(--border);border-radius:var(--r);padding:18px 20px;position:relative;transition:transform .18s,box-shadow .18s;animation:rowIn .25s ease both}
.sig-card:hover{transform:translateY(-2px);box-shadow:0 8px 32px rgba(0,0,0,.55)}
.sig-card.sc-buy{border-left:3px solid var(--green)}
.sig-card.sc-sell{border-left:3px solid var(--red)}
.sig-card.sc-hold{border-left:3px solid var(--blue)}

.sig-card-head{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:12px;gap:8px}
.sig-card-left{display:flex;align-items:center;flex-wrap:wrap;gap:6px}
.sig-card-ticker{font-size:20px;font-weight:700;letter-spacing:-.5px}
.sig-card-when{font-size:11px;color:var(--text3);white-space:nowrap;padding-top:2px}

.sig-card-opt{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:var(--rs);padding:10px 12px;margin-bottom:10px}
.sig-card-opt-title{font-size:12px;font-weight:600;letter-spacing:-.1px;margin-bottom:8px;color:var(--text2)}
.sig-card-metas{display:flex;flex-wrap:wrap;gap:12px}
.ometa{font-size:11px}
.ometa-l{display:block;font-size:9.5px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--text3);margin-bottom:1px}
.ometa-v{color:var(--text2);font-variant-numeric:tabular-nums}

.sig-card-prices{display:flex;align-items:center;gap:12px;font-size:12px;color:var(--text3);margin-bottom:10px}
.sig-card-prices span{color:var(--text2);font-variant-numeric:tabular-nums}

.sig-card-outcomes{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.sco-item{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--text3)}
.sco-label{font-size:10px;font-weight:600;letter-spacing:.06em;text-transform:uppercase}

/* empty / loading states */
.empty{color:var(--text3);font-size:13px;padding:32px 0;text-align:center}
.empty-cell{color:var(--text3);font-size:13px;padding:40px 24px !important;text-align:center}
@keyframes rowIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}

/* ── HISTORY PAGE ──────────────────────────────────────────────────────────── */

/* history summary metric row */
.metrics-row{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.metric-card{background:var(--glass);border:1px solid var(--border);border-radius:var(--r);box-shadow:var(--shadow);padding:20px 22px 18px;position:relative;overflow:hidden;transition:transform .2s,box-shadow .2s;cursor:default}
.metric-card:hover{transform:translateY(-2px);box-shadow:0 8px 32px rgba(0,0,0,.6)}
.metric-card::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--blue);border-radius:var(--r) var(--r) 0 0}
.metric-card.c-green::after{background:var(--green)}.metric-card.c-red::after{background:var(--red)}.metric-card.c-yellow::after{background:var(--yellow)}
.metric-label{font-size:10.5px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--text3);margin-bottom:8px}
.metric-value{font-size:34px;font-weight:200;letter-spacing:-1.4px;color:var(--text);line-height:1;font-variant-numeric:tabular-nums}
.metric-card.c-green .metric-value{color:var(--green)}.metric-card.c-red .metric-value{color:var(--red)}
.metric-sub{font-size:11px;color:var(--text3);margin-top:6px;letter-spacing:.01em}

/* filter row */
.filter-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.filter-btn{padding:6px 16px;border-radius:8px;font-size:12px;font-weight:500;color:var(--text2);cursor:pointer;border:1px solid var(--border);background:var(--glass);transition:all .15s;white-space:nowrap}
.filter-btn.active{background:var(--blue);color:#fff;border-color:var(--blue)}
.filter-btn:hover:not(.active){background:rgba(255,255,255,.08);color:var(--text)}
.filter-count{font-size:11px;color:var(--text3);margin-left:auto}

/* date groups */
.date-group{border:1px solid var(--border);border-radius:var(--rs);overflow:hidden;background:var(--glass)}
.dg-header{display:flex;align-items:center;justify-content:space-between;padding:13px 18px;background:rgba(255,255,255,.035);cursor:pointer;user-select:none;transition:background .15s;gap:12px}
.dg-header:hover{background:rgba(255,255,255,.065)}
.dg-header-left{display:flex;align-items:center;gap:12px}
.dg-date{font-size:13px;font-weight:600;letter-spacing:-.2px}
.dg-count{font-size:11px;color:var(--text3)}
.dg-outcomes{display:flex;gap:8px}
.dg-oc{font-size:10.5px;padding:1px 7px;border-radius:100px;font-weight:600}
.dg-oc.win{background:rgba(48,209,88,.12);color:var(--green)}
.dg-oc.loss{background:rgba(255,69,58,.12);color:var(--red)}
.dg-oc.pending{background:rgba(10,132,255,.12);color:var(--blue)}
.dg-arrow{color:var(--text3);font-size:10px;transition:transform .2s;flex-shrink:0}
.dg-header.collapsed .dg-arrow{transform:rotate(-90deg)}
.dg-body{overflow:hidden}

/* performance table */
.table-scroll{overflow-x:auto;margin:0 -2px;padding:0 2px}
.perf-table{width:100%;border-collapse:collapse;font-size:12.5px;min-width:760px}
.perf-table thead th{font-size:10.5px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--text3);padding:8px 14px 10px;text-align:left;border-bottom:1px solid rgba(255,255,255,.07);white-space:nowrap}
.perf-table thead th:first-child{padding-left:20px}
.perf-table tbody tr{transition:background .12s;cursor:default;animation:rowIn .25s ease both}
.perf-table tbody tr:hover td{background:rgba(255,255,255,.03)}
.perf-table tbody td{padding:9px 14px;border-bottom:1px solid rgba(255,255,255,.04);vertical-align:middle;white-space:nowrap;color:var(--text)}
.perf-table tbody td:first-child{padding-left:18px}
.perf-table tbody tr.t-win td:first-child{box-shadow:inset 3px 0 0 var(--green)}
.perf-table tbody tr.t-loss td:first-child{box-shadow:inset 3px 0 0 var(--red)}
.perf-table tbody tr.t-pending td:first-child{box-shadow:inset 3px 0 0 var(--blue)}
.perf-table tbody tr.t-win{background:rgba(48,209,88,.025)}
.perf-table tbody tr.t-loss{background:rgba(255,69,58,.025)}
.perf-table tbody tr.t-pending{background:rgba(10,132,255,.02)}
.ticker-name{font-size:13.5px;font-weight:600;letter-spacing:-.2px}

/* ── TODAY VIEW TOGGLE ────────────────────────────────────────────────────── */
.view-toggle{display:inline-flex;align-items:center;gap:4px;background:rgba(255,255,255,.06);border:1px solid var(--border);border-radius:10px;padding:3px}
.vt-btn{padding:5px 15px;border-radius:7px;font-size:12px;font-weight:500;color:var(--text2);cursor:pointer;border:none;background:transparent;transition:all .15s;white-space:nowrap}
.vt-btn.active{background:rgba(255,255,255,.12);color:var(--text);box-shadow:0 1px 4px rgba(0,0,0,.4)}
.vt-btn:hover:not(.active){color:var(--text)}
.today-view-header{display:flex;align-items:center;justify-content:flex-start}

/* ── STOCKS TABLE VIEW ────────────────────────────────────────────────────── */
.stocks-table{width:100%;border-collapse:collapse;font-size:12.5px;min-width:560px}
.stocks-table thead th{font-size:10.5px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--text3);padding:8px 14px 10px;text-align:left;border-bottom:1px solid rgba(255,255,255,.07);white-space:nowrap}
.stocks-table thead th:first-child{padding-left:20px}
.stocks-table tbody tr{transition:background .12s;animation:rowIn .25s ease both}
.stocks-table tbody tr:hover td{background:rgba(255,255,255,.03)}
.stocks-table tbody td{padding:10px 14px;border-bottom:1px solid rgba(255,255,255,.04);vertical-align:middle;white-space:nowrap;color:var(--text)}
.stocks-table tbody td:first-child{padding-left:18px}
.stocks-table tbody tr.sv-buy td:first-child{box-shadow:inset 3px 0 0 var(--green)}
.stocks-table tbody tr.sv-sell td:first-child{box-shadow:inset 3px 0 0 var(--red)}
.sv-ticker-big{font-size:15px;font-weight:700;letter-spacing:-.4px}
.sv-opts-note{font-size:10px;color:var(--blue);margin-top:3px}
.sv-strength-wrap{display:flex;flex-direction:column;gap:3px}
.sv-strength-track{height:3px;background:rgba(255,255,255,.08);border-radius:100px;overflow:hidden;width:56px}
.sv-strength-fill{height:100%;background:linear-gradient(90deg,var(--blue),var(--green));border-radius:100px}

/* ── Responsive ──────────────────────────────────────────────────────────── */
@media(max-width:960px){
  .metrics-row{grid-template-columns:repeat(2,1fr)}
  .today-grid{grid-template-columns:1fr}
}
@media(max-width:540px){
  .metrics-row{grid-template-columns:1fr}
  .main{padding:148px 14px 40px}
  .nav,.tab-bar{padding:0 14px}
  .tab-btn{padding:6px 14px;font-size:12px}
}
</style>
</head>
<body>

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
    <span class="nav-stat" id="navLastRun"></span>
    <span class="nav-stat" id="navNextRun"></span>
    <span class="nav-stat" id="navRunStats"></span>
    <div class="prices-live">
      <div class="pl-dot" id="plDot"></div>
      <span>Prices live</span>
    </div>
    <span class="nav-time" id="navTime">—</span>
  </div>
</nav>

<!-- Tab bar -->
<div class="tab-bar">
  <button class="tab-btn active" id="tabToday" onclick="switchTab('today')">Today's Signals</button>
  <button class="tab-btn" id="tabHist"  onclick="switchTab('history')">Performance History</button>
</div>

<!-- Ticker bar -->
<div class="ticker-bar">
  <div class="ticker-track" id="tickerTrack">
    <span class="ti-item"><span class="ti-ticker" style="color:var(--text3);font-weight:400">Fetching prices…</span></span>
  </div>
</div>

<!-- ── PAGE 1: TODAY'S SIGNALS ─────────────────────────────────────────────── -->
<main class="main" id="todayPage">

  <!-- market banner -->
  <div class="market-banner" id="marketBanner" style="display:none">
    <span class="market-banner-icon">🔴</span>
    <div class="market-banner-text" id="marketBannerText">Market Closed</div>
  </div>

  <!-- summary bar -->
  <div class="today-summary">
    <div class="ts-stat">
      <span class="ts-num ts-buy" id="tsBuy">0</span>
      <span class="ts-label">BUY</span>
    </div>
    <div class="ts-div"></div>
    <div class="ts-stat">
      <span class="ts-num ts-sell" id="tsSell">0</span>
      <span class="ts-label">SELL</span>
    </div>
    <div class="ts-div"></div>
    <div class="ts-stat">
      <span class="ts-num" id="tsTotal" style="color:var(--text2)">0</span>
      <span class="ts-label">TOTAL</span>
    </div>
  </div>

  <!-- view toggle -->
  <div class="today-view-header">
    <div class="view-toggle">
      <button class="vt-btn active" id="vtStocks" onclick="switchTodayView('stocks')">Stocks View</button>
      <button class="vt-btn" id="vtOptions" onclick="switchTodayView('options')">Options View</button>
    </div>
  </div>

  <!-- stocks list (default view) -->
  <div id="todayStocks"><p class="empty">Loading signals…</p></div>

  <!-- options cards -->
  <div class="today-grid" id="todayGrid" style="display:none"></div>

</main>

<!-- ── PAGE 2: PERFORMANCE HISTORY ────────────────────────────────────────── -->
<main class="main" id="histPage" style="display:none">

  <!-- aggregate stats -->
  <div class="metrics-row" id="histStats">
    <div class="metric-card" id="hmc0">
      <div class="metric-label">Overall Avg Return</div>
      <div class="metric-value" id="hmv0">—</div>
      <div class="metric-sub" id="hms0">resolved signals</div>
    </div>
    <div class="metric-card" id="hmc1">
      <div class="metric-label">Best Trade</div>
      <div class="metric-value" id="hmv1">—</div>
      <div class="metric-sub" id="hms1">top performer</div>
    </div>
    <div class="metric-card" id="hmc2">
      <div class="metric-label">Worst Trade</div>
      <div class="metric-value" id="hmv2">—</div>
      <div class="metric-sub" id="hms2">bottom performer</div>
    </div>
    <div class="metric-card" id="hmc3">
      <div class="metric-label">Total Hypo P&amp;L</div>
      <div class="metric-value" id="hmv3">—</div>
      <div class="metric-sub" id="hms3">1 contract each</div>
    </div>
  </div>

  <!-- closed trades summary -->
  <div class="card" id="closedSummaryCard" style="display:none">
    <div class="card-head">
      <span class="card-title">Closed Trades</span>
      <span class="card-badge" id="closedBadge">0 closed</span>
    </div>
    <div class="closed-summary" id="closedSummary"></div>
  </div>

  <!-- filter row -->
  <div class="card" style="padding:16px 22px">
    <div class="filter-row">
      <button class="filter-btn active" data-filter="ALL"     onclick="setFilter('ALL')">All</button>
      <button class="filter-btn"        data-filter="BUY"     onclick="setFilter('BUY')">BUY</button>
      <button class="filter-btn"        data-filter="SELL"    onclick="setFilter('SELL')">SELL</button>
      <button class="filter-btn"        data-filter="POSITIVE" onclick="setFilter('POSITIVE')">Positive</button>
      <button class="filter-btn"        data-filter="NEGATIVE" onclick="setFilter('NEGATIVE')">Negative</button>
      <button class="filter-btn"        data-filter="PENDING" onclick="setFilter('PENDING')">PENDING</button>
      <span class="filter-count" id="filterCount"></span>
    </div>
  </div>

  <!-- date groups -->
  <div id="histGroups"><p class="empty">Loading history…</p></div>

</main>

<script>
const PIPELINE_INTERVAL = 960000;
const PRICE_INTERVAL    = 30000;

let currentTab  = 'today';
let historyData = [];
let histFilter  = 'ALL';
let histLoaded  = false;
let openGroups  = new Set();
let todayView   = 'stocks';

// ── Utilities ────────────────────────────────────────────────────────────────

async function get(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) { console.error('API error', url, r.status, r.statusText); return null; }
    return r.json();
  } catch(e) { console.error('Fetch failed', url, e.message); return null; }
}

function fmt$(v) { return v == null ? '<span class="neu">—</span>' : '$' + Number(v).toFixed(2); }

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
    if (s < 60)    return s + "s ago";
    if (s < 3600)  return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  } catch { return '—'; }
}

function fmtGroupDate(dateStr) {
  try {
    const d = new Date(dateStr + 'T12:00:00Z');
    return d.toLocaleDateString('en-US', { month: 'long', day: '2-digit', year: 'numeric', timeZone: 'UTC' });
  } catch { return dateStr; }
}

// ── Today view toggle ────────────────────────────────────────────────────────

function switchTodayView(v) {
  todayView = v;
  document.getElementById('vtStocks').classList.toggle('active', v === 'stocks');
  document.getElementById('vtOptions').classList.toggle('active', v === 'options');
  document.getElementById('todayStocks').style.display = v === 'stocks' ? '' : 'none';
  document.getElementById('todayGrid').style.display   = v === 'options' ? '' : 'none';
}

// ── Tab switching ─────────────────────────────────────────────────────────────

function switchTab(tab) {
  currentTab = tab;
  document.getElementById('todayPage').style.display = tab === 'today' ? '' : 'none';
  document.getElementById('histPage').style.display  = tab === 'history' ? '' : 'none';
  document.getElementById('tabToday').classList.toggle('active', tab === 'today');
  document.getElementById('tabHist').classList.toggle('active', tab === 'history');
  if (tab === 'history' && !histLoaded) {
    histLoaded = true;
    refreshHistory();
  }
}

// ── Nav / status ──────────────────────────────────────────────────────────────

async function refreshStats() {
  const d = await get('/api/stats');
  if (!d) {
    const pill = document.getElementById('statusPill');
    pill.className = "status-pill offline";
    document.getElementById('statusLabel').textContent = "Offline";
    return;
  }
  const pill = document.getElementById('statusPill');
  const lbl  = document.getElementById('statusLabel');
  pill.className = "status-pill" + (d.bot_state === "idle" ? " idle" : d.bot_state !== "live" ? " offline" : "");
  lbl.textContent = d.bot_state === "live" ? "Live" : d.bot_state === "idle" ? "Idle" : "Offline";

  if (d.minutes_since_heartbeat != null) {
    const m = d.minutes_since_heartbeat;
    document.getElementById('navLastRun').textContent = "Last run: " + (m < 1 ? "just now" : Math.round(m) + "m ago");
  }
  if (d.minutes_until_next_run != null) {
    const n = d.minutes_until_next_run;
    document.getElementById('navNextRun').textContent = "Next: " + (n < 1 ? "<1m" : Math.round(n) + "m");
  }
  if (d.last_posts_collected != null || d.last_signals_generated != null)
    document.getElementById('navRunStats').textContent = (d.last_posts_collected ?? "—") + " posts · " + (d.last_signals_generated ?? "—") + " signals";
  document.getElementById('navTime').textContent = d.last_updated || '—';

  // Closed trades summary card
  const cs = d.closed_summary;
  if (cs && cs.total > 0) {
    document.getElementById('closedSummaryCard').style.display = '';
    document.getElementById('closedBadge').textContent = cs.total + " closed";
    const avgP = cs.avg_profit_pct != null ? " (avg +" + cs.avg_profit_pct.toFixed(1) + "%)" : "";
    const avgL = cs.avg_loss_pct   != null ? " (avg "  + cs.avg_loss_pct.toFixed(1) + "%)"  : "";
    document.getElementById('closedSummary').innerHTML = `
      <div class="cs-item"><div class="cs-num" style="color:var(--text)">${cs.total}</div><div class="cs-label">Total Closed</div></div>
      <div class="cs-item"><div class="cs-num" style="color:var(--green)">${cs.profit}</div><div class="cs-label">At Profit</div><div class="cs-sub">${avgP}</div></div>
      <div class="cs-item"><div class="cs-num" style="color:var(--red)">${cs.loss}</div><div class="cs-label">At Loss</div><div class="cs-sub">${avgL}</div></div>
      <div class="cs-item"><div class="cs-num" style="color:var(--blue)">${cs.active}</div><div class="cs-label">Still Active</div></div>
    `;
  }
}

// ── Today's Signals ───────────────────────────────────────────────────────────

function strengthBar(tw) {
  const pct = Math.min(100, Math.round((tw / 60) * 100));
  const label = tw >= 40 ? "Strong" : tw >= 20 ? "Moderate" : "Weak";
  return `<div class="strength-bar-wrap">
    <div class="strength-bar-label"><span>Signal Strength</span><span>${label} (${Math.round(tw)} wt)</span></div>
    <div class="strength-track"><div class="strength-fill" style="width:${pct}%"></div></div>
  </div>`;
}

function statusBadge(r) {
  const ts = r.trade_status || 'PENDING';
  if (ts === 'CLOSED_PROFIT')   return '<span class="b b-closed-profit">CLOSED PROFIT</span>';
  if (ts === 'CLOSED_LOSS')     return '<span class="b b-closed-loss">CLOSED LOSS</span>';
  if (ts === 'CLOSED_EXPIRY')   return '<span class="b b-closed-expiry">EXPIRED</span>';
  if (ts === 'CLOSED_REVERSAL') return '<span class="b b-closed-reversal">REVERSED</span>';
  if (ts === 'ACTIVE')          return '<span class="b b-active">ACTIVE</span>';
  return '';
}

async function refreshToday() {
  const data = await get('/api/today');
  if (!data) {
    const errMsg = '<p class="empty" style="color:var(--red)">Could not load — server may be offline.</p>';
    document.getElementById('todayGrid').innerHTML = errMsg;
    document.getElementById('todayStocks').innerHTML = errMsg;
    return;
  }

  const { signals, market_open, next_open, is_weekend, is_after_hours } = data;

  // market banner
  const banner = document.getElementById('marketBanner');
  const bannerText = document.getElementById('marketBannerText');
  if (is_weekend) {
    banner.style.display = 'flex';
    bannerText.innerHTML = `Markets Closed — Weekend Discussion Active<span> · Next open ${next_open || "Monday 9:30am ET"}</span>`;
  } else if (is_after_hours) {
    banner.style.display = 'flex';
    bannerText.innerHTML = `Markets Closed — After Hours<span> · Signals are for tomorrow's open · Next open ${next_open || "Tomorrow 9:30am ET"}</span>`;
  } else if (market_open) {
    banner.style.display = 'flex';
    banner.style.background = 'rgba(48,209,88,.07)';
    banner.style.borderColor = 'rgba(48,209,88,.2)';
    banner.style.color = 'var(--green)';
    document.querySelector('.market-banner-icon').textContent = '🟢';
    bannerText.innerHTML = "Markets Open — <span>Live Signals</span>";
  } else {
    banner.style.display = 'none';
  }

  // summary counts (BUY/SELL only — no HOLD)
  const buys  = signals.filter(s => s.signal === 'BUY').length;
  const sells = signals.filter(s => s.signal === 'SELL').length;
  document.getElementById('tsBuy').textContent   = buys;
  document.getElementById('tsSell').textContent  = sells;
  document.getElementById('tsTotal').textContent = signals.length;

  const grid     = document.getElementById('todayGrid');
  const stocksEl = document.getElementById('todayStocks');
  if (!signals.length) {
    const msg = '<p class="empty">No signals in the last 24 hours. The bot analyzes new signals every ~15 minutes.</p>';
    grid.innerHTML = msg;
    stocksEl.innerHTML = msg;
    return;
  }

  // ── Stocks view ────────────────────────────────────────────────────────────
  try {
    stocksEl.innerHTML = `<div class="card" style="padding:0 4px"><div class="table-scroll"><table class="stocks-table">
      <thead><tr><th>Ticker</th><th>Signal</th><th>Entry</th><th>Current</th><th>Change %</th><th>Strength</th><th>Posts</th></tr></thead>
      <tbody>${signals.map((r, i) => {
        const tk       = (r.ticker || '').replace(/[^A-Z0-9.]/g, '');
        const tw       = r.total_weight || 0;
        const entry    = r.price_at_signal != null ? '$' + Number(r.price_at_signal).toFixed(2) : '—';
        const pct      = Math.min(100, Math.round((tw / 60) * 100));
        const label    = tw >= 40 ? 'Strong' : tw >= 20 ? 'Moderate' : 'Weak';
        const posts    = r.signal_count || 1;
        const dir      = r.signal === 'BUY'
          ? '<span class="b b-buy">BUY</span>'
          : '<span class="b b-sell">SELL</span>';
        const optsNote = r.option_contract
          ? '<div class="sv-opts-note">Options available — click Options View for details</div>' : '';
        const svCls    = r.signal === 'BUY' ? 'sv-buy' : 'sv-sell';
        return `<tr class="${svCls}" style="animation-delay:${Math.min(i * 20, 400)}ms">
          <td><div class="sv-ticker-big">${r.ticker}</div>${optsNote}</td>
          <td>${dir}</td>
          <td class="mono">${entry}</td>
          <td class="mono price-cell" data-ticker="${tk}">—</td>
          <td class="mono chg-cell" data-ticker="${tk}">—</td>
          <td><div class="sv-strength-wrap"><span style="font-size:12px;font-weight:500">${label}</span><div class="sv-strength-track"><div class="sv-strength-fill" style="width:${pct}%"></div></div></div></td>
          <td class="mono">${posts}</td>
        </tr>`;
      }).join('')}</tbody>
    </table></div></div>`;
  } catch(e) {
    console.error('stocks view render error:', e);
    stocksEl.innerHTML = '<p class="empty" style="color:var(--red)">Render error: ' + e.message + '</p>';
  }

  // ── Options view ────────────────────────────────────────────────────────────
  try { grid.innerHTML = signals.map((r, i) => {
    const sigCls   = r.signal === 'BUY' ? 'sc-buy' : 'sc-sell';
    const dirBadge = r.signal === 'BUY'
      ? '<span class="b b-buy">BUY</span>'
      : '<span class="b b-sell">SELL</span>';
    const strongBadge = (r.confidence != null && r.confidence >= 0.80)
      ? '<span class="strong-badge">★ Strong Signal</span>' : '';
    const confirmBadge = (r.signal_count > 1)
      ? `<span class="confirm-badge">${r.signal_count} confirming signals</span>` : '';
    const ahBadge = r.after_hours
      ? '<span class="b-warn b" style="font-size:9.5px">After Hours</span>' : '';
    const when = timeAgo(r.signal_timestamp);
    const tk   = (r.ticker || '').replace(/[^A-Z0-9.]/g, '');
    const tw   = r.total_weight || 0;

    let optHtml = '';
    if (r.option_contract) {
      const strike = r.option_strike  ? '$' + Number(r.option_strike).toFixed(0) : '—';
      const expiry = r.option_expiry  || '—';
      const cost   = r.option_cost    ? '$' + Number(r.option_cost).toFixed(2)   : '—';
      const iv     = r.option_iv      ? (r.option_iv * 100).toFixed(0) + '%'     : '—';
      const delta  = r.option_delta   ? Number(r.option_delta).toFixed(2)        : '—';
      const ivCls  = r.option_iv && r.option_iv > 0.8 ? 'dn' : '';
      optHtml = `<div class="sig-card-opt">
        <div class="sig-card-opt-title">${r.option_type || '?'} ${strike} · exp ${expiry}</div>
        <div class="sig-card-metas">
          <div class="ometa"><span class="ometa-l">Cost</span><span class="ometa-v">${cost}</span></div>
          <div class="ometa"><span class="ometa-l">IV</span><span class="ometa-v ${ivCls}">${iv}</span></div>
          <div class="ometa"><span class="ometa-l">Delta</span><span class="ometa-v">${delta}</span></div>
          <div class="ometa"><span class="ometa-l">Contract</span><span class="ometa-v" style="font-size:10px">${r.option_contract}</span></div>
        </div>
      </div>`;
    }

    const entryStr = r.price_at_signal != null ? '$' + Number(r.price_at_signal).toFixed(2) : '—';
    const priceRow = `<div class="sig-card-prices">
      Entry <span>${entryStr}</span>
      &nbsp;·&nbsp; Live <span class="price-cell" data-ticker="${tk}">—</span>
      &nbsp;<span class="chg-cell" data-ticker="${tk}"></span>
    </div>`;

    let outcomes = [];
    if (r.percent_change_6h != null) {
      const cls  = r.outcome_6h === 'WIN' ? 'b-win' : 'b-loss';
      const sign = r.percent_change_6h >= 0 ? '+' : '';
      outcomes.push(`<span class="sco-item"><span class="sco-label">6h</span><span class="b ${cls}">${sign}${Number(r.percent_change_6h).toFixed(2)}%</span></span>`);
    } else {
      outcomes.push('<span class="sco-item"><span class="sco-label">6h</span><span class="b b-pending">PENDING</span></span>');
    }
    if (r.percent_change_24h != null) {
      const cls  = r.outcome === 'WIN' ? 'b-win' : 'b-loss';
      const sign = r.percent_change_24h >= 0 ? '+' : '';
      outcomes.push(`<span class="sco-item"><span class="sco-label">24h</span><span class="b ${cls}">${sign}${Number(r.percent_change_24h).toFixed(2)}%</span></span>`);
    } else {
      outcomes.push('<span class="sco-item"><span class="sco-label">24h</span><span class="b b-pending">PENDING</span></span>');
    }
    const optRet = r.option_return_7d ?? r.option_return_24h ?? r.option_return_6h;
    if (optRet != null) {
      const sign      = optRet >= 0 ? '+' : '';
      const cls       = optRet >= 0 ? 'b-win' : 'b-loss';
      const optProfit = r.option_profit_7d ?? r.option_profit_24h ?? r.option_profit_6h;
      const profitStr = optProfit != null ? ' (' + (optProfit >= 0 ? '+$' : '-$') + Math.abs(optProfit).toFixed(0) + ')' : '';
      outcomes.push(`<span class="sco-item"><span class="sco-label">Opt</span><span class="b ${cls}">${sign}${optRet.toFixed(1)}%${profitStr}</span></span>`);
    }

    return `<div class="sig-card ${sigCls}" style="animation-delay:${Math.min(i * 20, 400)}ms">
      <div class="sig-card-head">
        <div class="sig-card-left">
          <span class="sig-card-ticker">${r.ticker}</span>
          ${dirBadge}${strongBadge}${ahBadge}${confirmBadge}
        </div>
        <span class="sig-card-when">${when}</span>
      </div>
      ${strengthBar(tw)}
      ${optHtml}
      ${priceRow}
      <div class="sig-card-outcomes">${outcomes.join('')}</div>
      ${statusBadge(r) ? '<div style="margin-top:8px">' + statusBadge(r) + '</div>' : ''}
    </div>`;
  }).join(''); } catch(e) {
    console.error('refreshToday render error:', e);
    grid.innerHTML = '<p class="empty" style="color:var(--red)">Render error: ' + e.message + ' — check browser console.</p>';
  }
}

// ── Performance History ───────────────────────────────────────────────────────

async function refreshHistory() {
  const rows = await get('/api/history');
  if (!rows) {
    document.getElementById('histGroups').innerHTML = '<p class="empty" style="color:var(--red)">Could not load /api/history — check browser console for details.</p>';
    return;
  }
  historyData = rows;
  try { renderHistStats(rows); } catch(e) {
    console.error('renderHistStats error:', e);
    document.getElementById('hms0').textContent = 'Render error: ' + e.message;
  }
  try { renderHistGroups(rows); } catch(e) {
    console.error('renderHistGroups error:', e);
    document.getElementById('histGroups').innerHTML = '<p class="empty" style="color:var(--red)">Render error: ' + e.message + '</p>';
  }
}

function renderHistStats(rows) {
  // Only count BUY/SELL for win rates
  const actionRows  = rows.filter(r => r.signal === 'BUY' || r.signal === 'SELL');
  const resolved6h  = actionRows.filter(r => r.percent_change_6h != null);
  const wins6h      = resolved6h.filter(r => r.outcome_6h === 'WIN').length;
  const wr6h        = resolved6h.length ? (wins6h / resolved6h.length * 100).toFixed(1) : null;
  const resolvedAny = actionRows.filter(r => r.percent_change_24h != null || r.percent_change_6h != null);

  function bestPct(r)  { return r.percent_change_24h ?? r.percent_change_6h ?? -Infinity; }
  function worstPct(r) { return r.percent_change_24h ?? r.percent_change_6h ?? Infinity; }

  const best  = resolvedAny.length ? resolvedAny.reduce((a,b) => bestPct(a)  >= bestPct(b)  ? a : b) : null;
  const worst = resolvedAny.length ? resolvedAny.reduce((a,b) => worstPct(a) <= worstPct(b) ? a : b) : null;

  let totalPnl = 0, hasPnl = false;
  actionRows.forEach(r => {
    const p = r.close_profit_dollars ?? r.option_profit_7d ?? r.option_profit_24h ?? r.option_profit_6h;
    if (p != null) { totalPnl += p; hasPnl = true; }
  });

  // Overall Avg Return — best available checkpoint per trade, direction-adjusted for SELL
  const hmc0 = document.getElementById('hmc0'), hmv0 = document.getElementById('hmv0'), hms0 = document.getElementById('hms0');
  document.querySelector('#hmc0 .metric-label').textContent = 'Overall Avg Return';
  const resolvedForAvg = actionRows.filter(r => r.percent_change_24h != null || r.percent_change_6h != null);
  // For SELL signals, effective return = -(raw percent change): price drop is positive
  const effectiveReturns = resolvedForAvg.map(r => {
    const raw = r.percent_change_24h ?? r.percent_change_6h;
    return r.signal === 'SELL' ? -raw : raw;
  });
  console.log('[Overall Avg Return] resolved trades:', resolvedForAvg.map((r, i) => ({
    ticker: r.ticker, signal: r.signal,
    raw_pct: r.percent_change_24h ?? r.percent_change_6h,
    effective_return: effectiveReturns[i]
  })));
  const avgRet = effectiveReturns.length
    ? effectiveReturns.reduce((s, v) => s + v, 0) / effectiveReturns.length
    : null;
  if (avgRet != null) {
    const sign = avgRet >= 0 ? '+' : '';
    hmv0.textContent = sign + avgRet.toFixed(1) + '%';
    hmc0.className = 'metric-card ' + (avgRet >= 0 ? 'c-green' : 'c-red');
    hms0.textContent = resolvedForAvg.length + " resolved signals";
  } else { hmv0.textContent = "N/A"; hmc0.className = "metric-card"; hms0.textContent = "no resolved signals"; }

  // Options win rate
  const optRows = actionRows.filter(r =>
    r.close_return_pct != null ||
    r.option_return_6h != null || r.option_return_24h != null || r.option_return_7d != null
  );
  const optWins = optRows.filter(r => (r.close_return_pct ?? r.option_return_7d ?? r.option_return_24h ?? r.option_return_6h ?? 0) > 0).length;
  const optWR   = optRows.length ? (optWins / optRows.length * 100).toFixed(1) : null;
  // Re-use metric-card 1 as Options Win Rate
  const hmc1 = document.getElementById('hmc1'), hmv1 = document.getElementById('hmv1'), hms1 = document.getElementById('hms1');
  document.querySelector('#hmc1 .metric-label').textContent = 'Options Win Rate';
  if (optWR != null) {
    hmv1.textContent = optWR + '%';
    hmc1.className = 'metric-card ' + (parseFloat(optWR) >= 50 ? 'c-green' : 'c-red');
    hms1.textContent = optRows.length + " resolved options";
  } else { hmv1.textContent = "N/A"; hmc1.className = "metric-card"; hms1.textContent = "Options win rate · no data"; }

  const hmc2 = document.getElementById('hmc2'), hmv2 = document.getElementById('hmv2'), hms2 = document.getElementById('hms2');
  document.querySelector('#hmc2 .metric-label').textContent = 'Best Call';
  if (best) {
    const p = bestPct(best); hmv2.textContent = best.ticker; hmc2.className = 'metric-card c-green';
    hms2.textContent = (p >= 0 ? "+" : "") + p.toFixed(2) + "% · " + best.signal;
  } else { hmv2.textContent = "N/A"; hmc2.className = "metric-card"; hms2.textContent = "no data"; }

  const hmc3 = document.getElementById('hmc3'), hmv3 = document.getElementById('hmv3'), hms3 = document.getElementById('hms3');
  if (hasPnl) {
    hmv3.textContent = (totalPnl >= 0 ? '+$' : '-$') + Math.abs(totalPnl).toFixed(0);
    hmc3.className = 'metric-card ' + (totalPnl >= 0 ? 'c-green' : 'c-red');
    hms3.textContent = "1 contract each";
  } else { hmv3.textContent = "N/A"; hmc3.className = "metric-card"; hms3.textContent = "no resolved options"; }
}

function renderHistGroups(rows) {
  if (!rows.length) {
    document.getElementById('histGroups').innerHTML = '<p class="empty">No performance data yet.</p>';
    document.getElementById('filterCount').textContent = '';
    return;
  }

  // group by date
  const groups = {};
  rows.forEach(r => {
    const date = (r.signal_timestamp || 'Unknown').slice(0, 10);
    if (!groups[date]) groups[date] = [];
    groups[date].push(r);
  });

  const sortedDates = Object.keys(groups).sort((a,b) => b.localeCompare(a));

  // open all groups by default if first load
  if (openGroups.size === 0) sortedDates.forEach(d => openGroups.add(d));

  const container = document.getElementById('histGroups');
  container.innerHTML = sortedDates.map(date => {
    const grpRows = groups[date].filter(r => r.signal === 'BUY' || r.signal === 'SELL');
    const wins    = grpRows.filter(r => r.outcome === 'WIN' || r.outcome_6h === 'WIN').length;
    const losses  = grpRows.filter(r => r.outcome === 'LOSS' || r.outcome_6h === 'LOSS').length;
    const pending = grpRows.filter(r => r.outcome === 'PENDING' && r.outcome_6h !== 'WIN' && r.outcome_6h !== 'LOSS').length;
    const isOpen  = openGroups.has(date);

    const winChip     = wins    ? `<span class="dg-oc win">${wins} WIN</span>`      : '';
    const lossChip    = losses  ? `<span class="dg-oc loss">${losses} LOSS</span>`  : '';
    const pendingChip = pending ? `<span class="dg-oc pending">${pending} PENDING</span>` : '';

    const tableRows = grpRows.filter(r => r.signal === 'BUY' || r.signal === 'SELL').map((r, i) => {
      // Stock outcome drives filter tags — never classify 0% as LOSS
      const stockOut6  = r.outcome_6h || 'PENDING';
      const stockOut24 = r.outcome    || 'PENDING';
      const stockEff   = stockOut24 !== 'PENDING' ? stockOut24 : stockOut6;
      // Use direction-aware outcome for filter (WIN = positive return for both BUY and SELL)
      const outData    = stockEff === 'WIN' ? 'POSITIVE' : stockEff === 'LOSS' ? 'NEGATIVE' : 'PENDING';

      // Trade status takes priority for row color when closed
      const tStatus = r.trade_status || 'PENDING';
      let trCls = 't-pending';
      if (tStatus === 'CLOSED_PROFIT')   trCls = 't-win';
      else if (tStatus === 'CLOSED_LOSS') trCls = 't-loss';
      else if (stockEff === 'WIN')        trCls = 't-win';
      else if (stockEff === 'LOSS')       trCls = 't-loss';

      const dirBadge = r.signal === 'BUY'
        ? '<span class="b b-buy">BUY</span>'
        : '<span class="b b-sell">SELL</span>';

      // Outcome/status badge: prefer trade_status when closed
      let outBadge;
      if (tStatus === 'CLOSED_PROFIT')   outBadge = '<span class="b b-closed-profit">CLOSED PROFIT</span>';
      else if (tStatus === 'CLOSED_LOSS') outBadge = '<span class="b b-closed-loss">CLOSED LOSS</span>';
      else if (tStatus === 'CLOSED_EXPIRY') outBadge = '<span class="b b-closed-expiry">EXPIRED</span>';
      else if (tStatus === 'CLOSED_REVERSAL') outBadge = '<span class="b b-closed-reversal">REVERSED</span>';
      else if (tStatus === 'ACTIVE')      outBadge = '<span class="b b-active">ACTIVE</span>';
      else if (stockEff === 'WIN' || stockEff === 'LOSS') {
        const p = r.percent_change_24h ?? r.percent_change_6h;
        if (p != null) {
          const cls = p >= 0 ? 'up' : 'dn';
          outBadge = `<span class="${cls}">${p >= 0 ? '+' : ''}${Number(p).toFixed(2)}%</span>`;
        } else { outBadge = '<span class="b b-pending">PENDING</span>'; }
      } else                              outBadge = '<span class="b b-pending">PENDING</span>';

      const optRet  = r.close_return_pct ?? r.option_return_7d ?? r.option_return_24h ?? r.option_return_6h;
      const optVal  = r.close_option_value ?? r.option_value_7d ?? r.option_value_24h ?? r.option_value_6h;
      const optCost = r.option_cost;
      const optSrc  = r.option_price_source;

      const opt = r.option_contract
        ? `<span title="${r.option_contract}">${r.option_type || '?'}&nbsp;$${r.option_strike || '?'}</span>`
        : '<span class="neu">—</span>';

      let optRetHtml = '<span class="neu">—</span>';
      if (optRet != null) {
        const profit = r.close_profit_dollars ?? (optVal != null && optCost != null ? optVal - optCost : null);
        const cls    = optRet >= 0 ? 'up' : 'dn';
        const sign   = optRet >= 0 ? '+' : '';
        let str = `<span class="${cls}">${sign}${optRet.toFixed(1)}%`;
        if (profit != null) str += `<br><small>${profit >= 0 ? '+$' : '-$'}${Math.abs(profit).toFixed(0)}</small>`;
        if (optSrc) {
          const srcMap = {live:'Live',intrinsic:'At Expiry',estimate:'Est.'};
          const srcCls = optSrc === 'live' ? 'up' : optSrc === 'intrinsic' ? 'dn' : 'neu';
          str += `<br><small class="${srcCls}" style="font-size:9px">${srcMap[optSrc]||optSrc}</small>`;
        }
        str += '</span>';
        optRetHtml = str;
      }

      const tk = (r.ticker || '').replace(/[^A-Z0-9.]/g, '');

      return `<tr class="${trCls} hist-row" data-signal="${r.signal || ''}" data-outcome="${outData}" style="animation-delay:${Math.min(i * 12, 300)}ms">
        <td><span class="ticker-name">${r.ticker}</span></td>
        <td>${dirBadge}</td>
        <td class="mono">${fmt$(r.price_at_signal)}</td>
        <td class="mono price-cell" data-ticker="${tk}"><span class="neu">—</span></td>
        <td class="mono">${fmtPct(r.percent_change_6h)}</td>
        <td class="mono">${fmtPct(r.percent_change_24h)}</td>
        <td class="mono">${fmtPct(r.percent_change_7d)}</td>
        <td class="mono">${opt}</td>
        <td class="mono">${fmtIV(r.option_iv)}</td>
        <td class="mono">${optRetHtml}</td>
        <td>${outBadge}</td>
      </tr>`;
    }).join('');

    return `<div class="date-group">
      <div class="dg-header${isOpen ? '' : ' collapsed'}" onclick="toggleGroup(this,'${date}')">
        <div class="dg-header-left">
          <span class="dg-date">${fmtGroupDate(date)}</span>
          <span class="dg-count">${grpRows.length} signal${grpRows.length !== 1 ? 's' : ''}</span>
          <div class="dg-outcomes">${winChip}${lossChip}${pendingChip}</div>
        </div>
        <span class="dg-arrow">&#9660;</span>
      </div>
      <div class="dg-body" id="dgb-${date}" style="display:${isOpen ? '' : 'none'}">
        <div class="table-scroll">
          <table class="perf-table">
            <thead><tr>
              <th>Ticker</th><th>Dir</th><th>Entry</th><th>Live</th>
              <th>6h</th><th>24h</th><th>7d</th>
              <th>Option</th><th>IV</th><th>Opt Ret</th><th>Outcome</th>
            </tr></thead>
            <tbody>${tableRows}</tbody>
          </table>
        </div>
      </div>
    </div>`;
  }).join('');

  applyHistFilter();
}

function toggleGroup(header, date) {
  const body = document.getElementById('dgb-' + date);
  if (!body) return;
  const isOpen = body.style.display !== 'none';
  body.style.display = isOpen ? 'none' : '';
  header.classList.toggle('collapsed', isOpen);
  if (isOpen) openGroups.delete(date); else openGroups.add(date);
}

function setFilter(f) {
  histFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === f));
  applyHistFilter();
}

function applyHistFilter() {
  let visible = 0;
  document.querySelectorAll('.hist-row').forEach(row => {
    const sig = row.dataset.signal;
    const out = row.dataset.outcome;
    let show = histFilter === 'ALL'
      || (histFilter === 'BUY'     && sig === 'BUY')
      || (histFilter === 'SELL'    && sig === 'SELL')
      || (histFilter === 'POSITIVE' && out === 'POSITIVE')
      || (histFilter === 'NEGATIVE' && out === 'NEGATIVE')
      || (histFilter === 'PENDING' && out === 'PENDING');
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  // hide empty date groups
  document.querySelectorAll('.date-group').forEach(grp => {
    const body = grp.querySelector('.dg-body');
    if (!body) return;
    const anyVisible = body.querySelector('.hist-row:not([style*="none"])');
    grp.style.display = anyVisible ? '' : 'none';
  });
  const total = historyData.length;
  document.getElementById('filterCount').textContent = histFilter === "ALL"
    ? total + " total"
    : visible + " of " + total;
}

// ── Ticker bar ────────────────────────────────────────────────────────────────

function updateTickerBar(data) {
  if (!data || !data.length) return;
  const track = document.getElementById('tickerTrack');
  const items = data.map(({ ticker, price, change_pct }) => {
    const isUp  = change_pct != null && change_pct >= 0;
    const isDn  = change_pct != null && change_pct < 0;
    const cls   = isDn ? 'dn' : isUp ? 'up' : 'neu';
    const arrow = isDn ? '▼' : isUp ? '▲' : '';
    const pStr  = price != null ? '$' + Number(price).toFixed(2) : '—';
    const cStr  = change_pct != null ? (change_pct >= 0 ? '+' : '') + Number(change_pct).toFixed(2) + '%' : '';
    return `<span class="ti-item"><span class="ti-ticker">${ticker}</span> <span class="${cls}">${arrow ? `<span class="ti-arrow">${arrow}</span> ` : ''}${pStr}</span>${cStr ? ` <span class="${cls}">${cStr}</span>` : ''}</span>`;
  }).join('<span class="ti-sep">·</span>');
  const sep = '<span class="ti-sep" style="padding:0 12px">·</span>';
  track.innerHTML = items + sep + items;
  const dur = Math.max(20, data.length * 5);
  track.style.animation = 'none';
  void track.offsetWidth;
  track.style.animation = `ticker-scroll ${dur}s linear infinite`;
}

// ── Live prices ───────────────────────────────────────────────────────────────

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
        : '<span class="neu">—</span>';
      const pHtml = price != null
        ? `<span class="${cls}">${arrow ? `<span class="ti-arrow">${arrow}</span> ` : ''}${pStr}</span>`
        : '<span class="neu">—</span>';
      document.querySelectorAll(`.price-cell[data-ticker="${ticker}"]`).forEach(el => { el.innerHTML = pHtml; });
      document.querySelectorAll(`.chg-cell[data-ticker="${ticker}"]`).forEach(el => { el.innerHTML = cStr; });
    });
    updateTickerBar(data);
  }
  const dot = document.getElementById('plDot');
  dot.classList.remove('flash'); void dot.offsetWidth; dot.classList.add('flash');
  setTimeout(() => dot.classList.remove('flash'), 700);
  kickProgress();
}

function kickProgress() {
  const el = document.getElementById('pgFill');
  el.classList.remove('run'); void el.offsetWidth; el.classList.add('run');
}

// ── Boot ──────────────────────────────────────────────────────────────────────

async function boot() {
  await Promise.all([refreshStats(), refreshToday()]);
  await refreshPrices();
}

boot();
setInterval(async () => {
  await refreshStats();
  if (currentTab === 'today') await refreshToday();
  else { await refreshHistory(); }
  await refreshPrices();
}, PIPELINE_INTERVAL);
setInterval(refreshPrices, PRICE_INTERVAL);
</script>
</body>
</html>"""
