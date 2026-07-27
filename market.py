"""
Market data access — every yfinance call in the app goes through here.

Split out of app.py so that the explorer UI and the paper-trading playground can
both reach the data layer without importing each other.

Everything that can fail against the provider raises DataError, so callers can
show a message instead of a traceback.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import yfinance as yf

# yfinance chatters to stderr/stdout about missing data; we surface our own messages.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

SYMBOL_RE = re.compile(r"[A-Za-z0-9.\-^=]{1,15}")
# Spot has to go stale faster than the playground's refresh tick, or a re-render
# lands on a still-valid cache entry and the price appears frozen for two cycles.
# See playground.LIVE_REFRESH_SECONDS, which must stay above this.
SPOT_TTL = 15  # seconds
# Chains are a much bigger payload and move more slowly than the last trade.
CHAIN_TTL = 60
EXPIRY_TTL = 900

PUTS, CALLS = "puts", "calls"

NY = ZoneInfo("America/New_York")
# yfinance's daily bar isn't final the instant the bell rings, so wait a little
# past 4pm ET before treating a close as the settlement price.
SETTLE_BUFFER_MIN = 30
# A settlement price more than this many calendar days before expiration means
# the ticker stopped trading; refuse rather than settle against stale data.
MAX_SETTLE_STALENESS_DAYS = 5


class DataError(Exception):
    """Anything that means 'we can't show a quote for this row'."""


def normalize_symbol(raw: str) -> str:
    return raw.strip().upper()


def valid_symbol(symbol: str) -> bool:
    return bool(symbol) and SYMBOL_RE.fullmatch(symbol) is not None


def pretty_date(iso: str) -> str:
    try:
        dt = datetime.strptime(iso, "%Y-%m-%d")
    except ValueError:
        return iso
    return f"{dt:%b} {dt.day}, {dt:%Y}"


def now_et() -> datetime:
    """Wall clock in the market's timezone, regardless of the machine's."""
    return datetime.now(NY)


def market_close_passed(expiration: str, now: datetime | None = None) -> bool:
    """Has the 4pm ET close on `expiration` (plus a settling buffer) gone by?

    Options stop trading at the close on their expiration date, so this is the
    gate for settlement: before it, a price fetch would return an in-progress
    intraday bar rather than the final close.
    """
    try:
        exp = date.fromisoformat(expiration)
    except ValueError:
        return False
    cutoff = datetime.combine(exp, time(16, 0), tzinfo=NY) + timedelta(minutes=SETTLE_BUFFER_MIN)
    return (now or now_et()) >= cutoff


# --------------------------------------------------------------------------- #
# Cached fetchers (the Refresh button clears the quote caches, not settlement)
# --------------------------------------------------------------------------- #


@st.cache_data(ttl=EXPIRY_TTL, show_spinner=False)
def fetch_expirations(symbol: str) -> tuple[str, ...]:
    try:
        expirations = tuple(yf.Ticker(symbol).options or ())
    except Exception as exc:  # noqa: BLE001 - yfinance raises a grab-bag of errors
        raise DataError(f"Could not reach data provider for {symbol}: {exc}") from exc
    if not expirations:
        raise DataError(
            f"No options expirations found for {symbol}. "
            "The ticker may be invalid or it may not have listed options."
        )
    return expirations


@st.cache_data(ttl=SPOT_TTL, show_spinner=False)
def fetch_spot(symbol: str) -> float:
    ticker = yf.Ticker(symbol)
    price = None
    try:
        fast = ticker.fast_info
        price = fast.get("last_price") or fast.get("lastPrice")
    except Exception:  # noqa: BLE001 - fall through to the history lookup
        price = None

    if price is None:
        try:
            history = ticker.history(period="1d")
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"Could not fetch a spot price for {symbol}: {exc}") from exc
        if history is None or history.empty:
            raise DataError(f"No recent price data returned for {symbol}.")
        price = float(history["Close"].iloc[-1])

    price = float(price)
    if not price > 0:
        raise DataError(f"Spot price for {symbol} came back as {price}.")
    return price


@st.cache_data(ttl=CHAIN_TTL, show_spinner=False)
def fetch_chain(symbol: str, expiration: str, kind: str) -> pd.DataFrame:
    try:
        chain = yf.Ticker(symbol).option_chain(expiration)
    except Exception as exc:  # noqa: BLE001 - provider errors can be very verbose
        detail = str(exc).split("Available expirations")[0].strip().rstrip(".")
        raise DataError(f"No options chain for {symbol} @ {expiration}: {detail}.") from exc

    table = chain.puts if kind == PUTS else chain.calls
    if table is None or table.empty:
        raise DataError(f"The {kind[:-1]} chain for {symbol} @ {expiration} is empty.")
    return table.copy()


@st.cache_data(ttl=CHAIN_TTL, show_spinner=False)
def fetch_strikes(symbol: str, expiration: str, kind: str) -> list[float]:
    """Every strike listed for this expiration, bid or no bid."""
    return sorted(float(s) for s in fetch_chain(symbol, expiration, kind)["strike"])


@st.cache_data(ttl=CHAIN_TTL, show_spinner=False)
def fetch_quotable_strikes(symbol: str, expiration: str, kind: str) -> list[float]:
    """Strikes carrying a live bid — the ones you could actually sell right now.

    Far out-of-the-money strikes stay listed long after anyone stops quoting
    them, so the full listed range overstates what's tradeable. This is the
    range worth showing.
    """
    chain = fetch_chain(symbol, expiration, kind)
    if "bid" not in chain.columns:
        return []
    bids = pd.to_numeric(chain["bid"], errors="coerce")
    quotable = chain.loc[bids.notna() & (bids > 0), "strike"]
    return sorted(float(s) for s in quotable)


@st.cache_data(ttl=None, show_spinner=False, max_entries=1024)
def fetch_settlement_close(symbol: str, expiration: str) -> tuple[float, str, str]:
    """The closing price that settles an option expiring on `expiration`.

    Returns (close, the ISO date that close came from, a note for the user).
    Raises DataError whenever the price isn't determinable *yet* — the caller
    leaves the position pending and retries later. It must never fall back to a
    placeholder, because a wrong settlement silently books a wrong balance.

    Cached forever on purpose: a past date's close never changes. Streamlit does
    not cache exceptions, so transient provider failures retry on their own.
    """
    if not market_close_passed(expiration):
        # Guarded again at the call site; belt and braces, because caching an
        # intraday price under ttl=None would poison this entry for the process.
        raise DataError(
            f"{symbol} {expiration} hasn't settled yet — options settle after "
            "the 4:00 pm ET close on the expiration date."
        )

    exp = date.fromisoformat(expiration)
    try:
        bars = yf.Ticker(symbol).history(
            start=(exp - timedelta(days=10)).isoformat(),
            end=(exp + timedelta(days=1)).isoformat(),  # `end` is exclusive
        )
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"Could not fetch a settlement price for {symbol}: {exc}") from exc

    if bars is None or bars.empty or "Close" not in bars.columns:
        raise DataError(f"No price history for {symbol} around {pretty_date(expiration)}.")

    # The index is tz-aware (America/New_York); comparing it against a naive
    # Timestamp raises, so match on calendar dates instead.
    bar_dates = bars.index.date

    exact = bars.loc[bar_dates == exp]
    if not exact.empty:
        close, used, note = float(exact["Close"].iloc[-1]), exp, ""
    else:
        # Expiration on a weekend or holiday, or the ticker was halted.
        earlier = bars.loc[bar_dates < exp]
        if earlier.empty:
            raise DataError(
                f"No {symbol} trading day on or before {pretty_date(expiration)}."
            )
        used = earlier.index[-1].date()
        close = float(earlier["Close"].iloc[-1])
        if (exp - used).days > MAX_SETTLE_STALENESS_DAYS:
            raise DataError(
                f"The last {symbol} price before {pretty_date(expiration)} is from "
                f"{pretty_date(used.isoformat())} — too stale to settle against."
            )
        note = (f"No {symbol} bar on {pretty_date(expiration)}; settled at the "
                f"{pretty_date(used.isoformat())} close.")

    if not math.isfinite(close) or close <= 0:
        raise DataError(f"{symbol} close for {used} came back as {close}.")
    return close, used.isoformat(), note


def spots_for(symbols: Iterable[str]) -> dict[str, float | None]:
    """Current price per distinct symbol; None where the fetch failed.

    Never raises — the playground needs to render even when a ticker is broken.
    """
    spots: dict[str, float | None] = {}
    for symbol in dict.fromkeys(symbols):  # de-duplicate, keep order
        try:
            spots[symbol] = fetch_spot(symbol)
        except DataError:
            spots[symbol] = None
        except Exception:  # noqa: BLE001 - a bad ticker must not blank the page
            spots[symbol] = None
    return spots


def clear_quote_caches() -> None:
    """Drop the live-quote caches.

    Deliberately not `st.cache_data.clear()`, which would also throw away
    `fetch_settlement_close` — settled prices are immutable and expensive to
    re-fetch, and history rows already displayed shouldn't churn.
    """
    fetch_expirations.clear()
    fetch_spot.clear()
    fetch_chain.clear()
    fetch_strikes.clear()
    fetch_quotable_strikes.clear()


# --------------------------------------------------------------------------- #
# Quote math
# --------------------------------------------------------------------------- #


def _num(value: Any) -> float | None:
    """yfinance leaves NaN/None all over the chain; normalize to float or None."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(out) else out


def liquidity(bid: float | None, open_interest: float | None, volume: float | None,
              min_oi: int, min_volume: int) -> tuple[str, str, str]:
    """Return (badge text, badge css class, explanatory note)."""
    if not bid or bid <= 0:
        return "No bid", "bad", "No live bid — this contract is not tradeable right now."

    oi, vol = open_interest or 0, volume or 0
    thin = []
    if oi < min_oi:
        thin.append(f"open interest {oi:,.0f} < {min_oi:,}")
    if vol < min_volume:
        thin.append(f"volume {vol:,.0f} < {min_volume:,}")
    if thin:
        return "Thin", "warn", "Low liquidity: " + " and ".join(thin) + "."
    return "Liquid", "ok", ""


def fetch_ask(symbol: str, expiration: str, strike: float, kind: str) -> float | None:
    """The current ask for one specific contract, or None if it isn't quoted.

    Reads through the cached chain, so several open positions sharing an
    expiration cost one fetch between them rather than one each.
    """
    chain = fetch_chain(symbol, expiration, kind)
    if "ask" not in chain.columns or chain.empty:
        return None
    strikes = chain["strike"].astype(float)
    row = chain.loc[(strikes - float(strike)).abs().idxmin()]
    if abs(float(row["strike"]) - float(strike)) > 1e-9:
        return None  # that exact strike is no longer listed
    return _num(row.get("ask"))


def price_contract(symbol: str, expiration: str, strike: float, kind: str,
                   min_oi: int, min_volume: int) -> dict:
    """Pull spot + the nearest listed contract. Raises DataError on bad input."""
    spot = fetch_spot(symbol)
    chain = fetch_chain(symbol, expiration, kind)

    strikes = chain["strike"].astype(float)
    contract = chain.loc[(strikes - float(strike)).abs().idxmin()]

    actual = float(contract["strike"])
    bid = _num(contract.get("bid"))
    ask = _num(contract.get("ask"))
    open_interest = _num(contract.get("openInterest"))
    volume = _num(contract.get("volume"))
    badge, badge_class, note = liquidity(bid, open_interest, volume, min_oi, min_volume)

    quote = {
        "strike": actual,
        "requested_strike": float(strike),
        "spot": spot,
        "bid": bid,
        "ask": ask,
        "open_interest": open_interest,
        "volume": volume,
        "badge": badge,
        "badge_class": badge_class,
        "note": note,
        "premium": (bid or 0.0) * 100,
        "fetched_at": datetime.now(),
    }
    if kind == PUTS:
        # Assignment happens below the strike, so the cushion is spot - strike.
        quote["cash_required"] = actual * 100
        quote["move"] = spot - actual
    else:
        # Called away above the strike, so the room to run is strike - spot.
        quote["move"] = actual - spot
    quote["move_pct"] = quote["move"] / spot * 100
    return quote
