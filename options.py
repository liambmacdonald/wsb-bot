"""
Options advisor — recommends a single options contract for each BUY/SELL signal.
Fetches live options chain data via yfinance.
"""

import logging
import math
from datetime import datetime

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_SEP            = "-" * 70
_EXPIRY_MIN     = 14      # primary window: min days to expiry
_EXPIRY_MAX     = 30      # primary window: max days to expiry
_FB_EXPIRY_MIN  = 7       # fallback window: min
_FB_EXPIRY_MAX  = 45      # fallback window: max
_OTM_PCT        = 0.05    # target 5% OTM
_MIN_OI         = 100
_MIN_BID        = 0.10
_IV_HIGH_THRESH    = 0.60
_IV_EXTREME_THRESH = 0.80


def _norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2))) / 2.0


def _bs_delta(S: float, K: float, T_years: float, iv: float, option_type: str) -> float | None:
    """Black-Scholes delta (risk-free rate = 0). Returns None on bad inputs."""
    if T_years <= 0 or iv <= 0 or S <= 0 or K <= 0:
        return None
    try:
        d1 = (math.log(S / K) + 0.5 * iv ** 2 * T_years) / (iv * math.sqrt(T_years))
        return _norm_cdf(d1) if option_type == "CALL" else _norm_cdf(d1) - 1.0
    except Exception:
        return None


def _nearest_expiry(expirations: tuple[str, ...], now: datetime) -> str | None:
    """Return the nearest expiry in the 14–30 day window, falling back to 7–45."""
    primary:  list[tuple[int, str]] = []
    fallback: list[tuple[int, str]] = []
    for exp in expirations:
        try:
            days = (datetime.strptime(exp, "%Y-%m-%d") - now).days
        except ValueError:
            continue
        if _EXPIRY_MIN <= days <= _EXPIRY_MAX:
            primary.append((days, exp))
        elif _FB_EXPIRY_MIN <= days <= _FB_EXPIRY_MAX:
            fallback.append((days, exp))
    if primary:
        return min(primary)[1]
    if fallback:
        return min(fallback)[1]
    return None


def _pick_contract(chain: pd.DataFrame, signal: str, current_price: float) -> dict | None:
    """Return the best contract: 5% OTM first, ATM as fallback."""
    if chain is None or chain.empty:
        return None

    df = chain.copy()
    df = df[(df["openInterest"] >= _MIN_OI) & (df["bid"] >= _MIN_BID)]
    if df.empty:
        return None

    if signal == "BUY":
        target = current_price * (1 + _OTM_PCT)
        otm = df[df["strike"] >= target].sort_values("strike", ascending=True)
    else:
        target = current_price * (1 - _OTM_PCT)
        otm = df[df["strike"] <= target].sort_values("strike", ascending=False)

    if not otm.empty:
        return otm.iloc[0].to_dict()

    # ATM fallback: closest strike to current price
    idx = (df["strike"] - current_price).abs().idxmin()
    return df.loc[idx].to_dict()


def estimate_option_return(
    current_price: float,
    strike: float,
    mid_price: float,
    target_pct_move: float,
    option_type: str = "CALL",
) -> float:
    """
    Estimate % return at expiry using intrinsic value only (simple model).
    Returns -100.0 when the option expires worthless.
    """
    if mid_price <= 0:
        return -100.0
    new_price = current_price * (1 + target_pct_move)
    if option_type == "CALL":
        intrinsic = max(0.0, new_price - strike)
    else:
        intrinsic = max(0.0, strike - new_price)
    if intrinsic == 0:
        return -100.0
    return (intrinsic - mid_price) / mid_price * 100


def get_options_recommendation(
    ticker: str,
    signal: str,
    current_price: float,
    confidence: float,
) -> dict | None:
    """
    Return a recommendation dict for a BUY/SELL signal, or None if no
    suitable contract is found.
    """
    if signal not in ("BUY", "SELL"):
        return None

    try:
        t           = yf.Ticker(ticker)
        expirations = t.options
    except Exception as exc:
        logger.warning("options: expiry fetch failed for %s: %s", ticker, exc)
        print(f"  [options] Warning: could not fetch options for {ticker} — skipping")
        return None

    if not expirations:
        logger.info("options: no expirations available for %s", ticker)
        return None

    expiry = _nearest_expiry(expirations, datetime.utcnow())
    if expiry is None:
        logger.info("options: no suitable expiry found for %s", ticker)
        return None

    try:
        chain     = t.option_chain(expiry)
        contracts = chain.calls if signal == "BUY" else chain.puts
    except Exception as exc:
        logger.warning("options: chain fetch failed for %s %s: %s", ticker, expiry, exc)
        print(f"  [options] Warning: could not fetch options chain for {ticker} — skipping")
        return None

    contract = _pick_contract(contracts, signal, current_price)
    if contract is None:
        logger.info(
            "options: no qualifying contract for %s %s (price=%.2f, expiry=%s)",
            signal, ticker, current_price, expiry,
        )
        return None

    option_type = "CALL" if signal == "BUY" else "PUT"
    strike      = float(contract.get("strike", 0))
    last_price  = float(contract.get("lastPrice", 0))
    bid         = float(contract.get("bid", 0))
    ask         = float(contract.get("ask", 0))
    iv          = float(contract.get("impliedVolatility", 0))
    oi          = int(contract.get("openInterest", 0))
    symbol      = str(contract.get("contractSymbol", ""))

    raw_delta = contract.get("delta")
    delta     = float(raw_delta) if raw_delta is not None else None
    if delta is None:
        try:
            days_to_expiry = (datetime.strptime(expiry, "%Y-%m-%d") - datetime.utcnow()).days
        except ValueError:
            days_to_expiry = 0
        T = max(days_to_expiry, 1) / 365.0
        delta = _bs_delta(current_price, strike, T, iv, option_type)

    mid      = (bid + ask) / 2 if bid > 0 and ask > 0 else last_price
    cost     = round(mid * 100, 2)
    breakeven = round(strike + mid, 2) if option_type == "CALL" else round(strike - mid, 2)

    if confidence >= 0.80:
        sizing = "Consider 1-2 contracts"
    elif confidence >= 0.60:
        sizing = "Consider 1 contract only"
    else:
        sizing = "Skip — insufficient confidence"

    cost_warning = (
        "WARNING: High cost per contract — consider lower strike or closer expiry to reduce premium"
        if cost > 500 else None
    )

    if iv > _IV_EXTREME_THRESH:
        iv_warning = "EXTREME IV — options very expensive, avoid"
    elif iv > _IV_HIGH_THRESH:
        iv_warning = "HIGH IV — options expensive, reduce size"
    else:
        iv_warning = None

    return {
        "ticker":             ticker,
        "signal":             signal,
        "option_type":        option_type,
        "contract_symbol":    symbol,
        "strike":             strike,
        "expiration":         expiry,
        "last_price":         last_price,
        "bid":                bid,
        "ask":                ask,
        "mid_price":          mid,
        "implied_volatility": iv,
        "delta":              delta,
        "open_interest":      oi,
        "cost_per_contract":  cost,
        "breakeven_price":    breakeven,
        "sizing_suggestion":  sizing,
        "iv_warning":         iv_warning,
        "cost_warning":       cost_warning,
        "current_price":      current_price,
        "confidence":         confidence,
    }


def format_recommendation(rec: dict) -> str:
    """Return a formatted console block for an options recommendation."""
    ticker   = rec["ticker"]
    signal   = rec["signal"]
    opt_type = rec["option_type"]
    strike   = rec["strike"]
    expiry   = rec["expiration"]
    bid      = rec["bid"]
    ask      = rec["ask"]
    mid      = rec["mid_price"]
    cost     = rec["cost_per_contract"]
    be       = rec["breakeven_price"]
    iv       = rec["implied_volatility"]
    oi       = rec["open_interest"]
    conf     = rec["confidence"]
    cp       = rec["current_price"]

    try:
        days_left = (datetime.strptime(expiry, "%Y-%m-%d") - datetime.utcnow()).days
    except ValueError:
        days_left = 0

    otm_pct = (
        (strike - cp) / cp * 100 if opt_type == "CALL"
        else (cp - strike) / cp * 100
    )

    if conf >= 0.80:
        sizing_line = f"Consider 1-2 contracts (${cost:.0f}-${2 * cost:.0f})"
    elif conf >= 0.60:
        sizing_line = f"Consider 1 contract only (${cost:.0f})"
    else:
        sizing_line = "Skip — insufficient confidence"

    lines = [
        _SEP,
        f"  OPTIONS RECOMMENDATION  —  {ticker} {signal}",
        _SEP,
        f"  {'Contract':<14}: {rec['contract_symbol']}",
        f"  {'Type':<14}: {opt_type}",
        f"  {'Strike':<14}: ${strike:.2f}  ({otm_pct:+.1f}% OTM)",
        f"  {'Expiration':<14}: {expiry}  ({days_left} days)",
        f"  {'Bid / Ask':<14}: ${bid:.2f} / ${ask:.2f}",
        f"  {'Cost (1 lot)':<14}: ${cost:.2f}  (max loss)",
        f"  {'Break Even':<14}: ${be:.2f} at expiry",
        f"  {'IV':<14}: {iv * 100:.1f}%",
        f"  {'Open Interest':<14}: {oi:,}",
        "",
    ]

    scenarios = (
        [("+10%", 0.10), ("+20%", 0.20), (" -5%", -0.05)]
        if opt_type == "CALL"
        else [("-10%", -0.10), ("-20%", -0.20), (" +5%",  0.05)]
    )
    for label, move in scenarios:
        pct = estimate_option_return(cp, strike, mid, move, opt_type)
        if pct <= -99.9:
            lines.append(f"  If stock moves {label} → option expires worthless (-100%)")
        else:
            lines.append(f"  If stock moves {label} → option est. {pct:+.0f}%")

    lines += [
        "",
        f"  Confidence {conf:.2f} → {sizing_line}",
    ]

    if rec.get("iv_warning"):
        lines.append(f"  *** {rec['iv_warning']} ***")
    if rec.get("cost_warning"):
        lines.append(f"  *** {rec['cost_warning']} ***")

    lines.append(_SEP)
    return "\n".join(lines)
