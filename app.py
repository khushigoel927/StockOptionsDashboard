"""
Options Selling Explorer — a live dashboard for eyeballing cash-secured puts
and covered calls.

Not a position tracker and not connected to a broker: rows are just "what would
this contract look like right now" scratch cards that live in st.session_state.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import pandas as pd
import streamlit as st
import yfinance as yf

# yfinance chatters to stderr/stdout about missing data; we surface our own messages.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

SYMBOL_RE = re.compile(r"[A-Za-z0-9.\-^=]{1,15}")
SPOT_TTL = 60  # seconds
CHAIN_TTL = 60
EXPIRY_TTL = 900

PUTS, CALLS = "puts", "calls"

CSS = """
<style>
.oc-head { display:flex; align-items:center; gap:0.75rem; }
.oc-avatar { width:3rem; height:3rem; border-radius:50%; flex:0 0 auto;
  background:rgba(70,110,190,0.28); color:#9dbcf5; display:flex;
  align-items:center; justify-content:center; font-size:0.68rem; font-weight:700;
  letter-spacing:0.02em; }
.oc-title { font-size:1.35rem; font-weight:700; line-height:1.2; }
.oc-sub { font-size:0.9rem; opacity:0.6; }
.oc-badge { display:inline-block; padding:0.3rem 0.85rem; border-radius:999px;
  font-size:0.85rem; font-weight:600; white-space:nowrap; }
.oc-badge.ok { background:rgba(45,180,110,0.18); color:#3dd68c; }
.oc-badge.warn { background:rgba(225,165,55,0.18); color:#e8b04b; }
.oc-badge.bad { background:rgba(225,80,80,0.18); color:#ff7676; }
.oc-badge-wrap { text-align:right; padding-top:0.6rem; }
.oc-metrics { display:grid; grid-template-columns:repeat(2,minmax(0,1fr));
  gap:0.85rem 2rem; }
.oc-cell.oc-wide { grid-column:1 / -1; }
.oc-lbl { font-size:0.82rem; opacity:0.55; margin-bottom:0.1rem; }
.oc-val { font-size:1.05rem; font-weight:600; }
.oc-val.good { color:#3dd68c; }
.oc-val.bad { color:#ff7676; }
.oc-spot { font-size:1rem; margin-top:0.35rem; }
.oc-spot-tick { font-weight:700; color:#9dbcf5; margin-right:0.15rem; }
.oc-spot-meta { opacity:0.6; font-size:0.88rem; }
</style>
"""


# --------------------------------------------------------------------------- #
# Data access (cached; the Refresh button clears these caches)
# --------------------------------------------------------------------------- #


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
    return sorted(float(s) for s in fetch_chain(symbol, expiration, kind)["strike"])


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


# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #


def init_state() -> None:
    st.session_state.setdefault(PUTS, [])
    st.session_state.setdefault(CALLS, [])
    st.session_state.setdefault("next_id", 1)
    st.session_state.setdefault("last_refresh", datetime.now())


def reprice(row: dict, kind: str, min_oi: int, min_volume: int) -> None:
    """Refresh a row in place from its committed strike/expiration."""
    try:
        row["quote"] = price_contract(row["symbol"], row["expiration"], row["strike"],
                                      kind, min_oi, min_volume)
        row["error"] = None
        # Snap the committed strike to the contract we actually found.
        row["strike"] = row["quote"]["strike"]
        st.session_state[f"pending_strike_{row['id']}"] = row["strike"]
    except DataError as exc:
        row["quote"] = None
        row["error"] = str(exc)


def add_row(kind: str, symbol: str, expiration: str, strike: float,
            min_oi: int, min_volume: int) -> tuple[bool, str]:
    rows = st.session_state[kind]
    if any(r["symbol"] == symbol and r["expiration"] == expiration
           and abs(r["strike"] - strike) < 1e-9 for r in rows):
        return False, f"{symbol} {pretty_date(expiration)} {strike:g} is already listed."

    row = {"id": st.session_state.next_id, "symbol": symbol,
           "expiration": expiration, "strike": float(strike),
           "quote": None, "error": None}
    st.session_state.next_id += 1
    reprice(row, kind, min_oi, min_volume)
    rows.append(row)
    return True, f"Added {symbol} {pretty_date(expiration)} {strike:g}."


# --------------------------------------------------------------------------- #
# UI — add form
# --------------------------------------------------------------------------- #


def nearest_strike(symbol: str, expiration: str, kind: str,
                   spot: float) -> float | None:
    """Closest listed strike to spot; None if the chain can't be read."""
    try:
        strikes = fetch_strikes(symbol, expiration, kind)
    except DataError:
        return None
    return min(strikes, key=lambda s: abs(s - spot)) if strikes else None


def spot_line(symbol: str, expiration: str, kind: str, spot: float | None) -> str:
    """The 'here's what the stock costs right now' line under the add form."""
    price = f"<b>${spot:,.2f}</b>" if spot else "<b>unavailable</b>"
    meta = ""
    try:
        strikes = fetch_strikes(symbol, expiration, kind)
        meta = (f" · {len(strikes)} strikes for {pretty_date(expiration)} "
                f"(${min(strikes):,.2f} – ${max(strikes):,.2f})")
        if spot:
            near = min(strikes, key=lambda s: abs(s - spot))
            meta += f" · nearest to spot <b>{near:g}</b>"
    except DataError:
        pass
    return (f'<div class="oc-spot"><span class="oc-spot-tick">{symbol}</span> '
            f'spot {price}<span class="oc-spot-meta">{meta}</span></div>')


def render_add_form(kind: str, min_oi: int, min_volume: int) -> None:
    label = "put" if kind == PUTS else "call"
    symbol_key, strike_key = f"{kind}_symbol", f"{kind}_strike"

    # Widget state can only be poked before the widget is created, so a reset
    # requested by the previous run is applied here.
    st.session_state.setdefault(strike_key, None)
    if st.session_state.pop(f"{kind}_reset_strike", False):
        st.session_state[strike_key] = None

    with st.container(border=True):
        st.markdown(f"### Add {label}")

        symbol = normalize_symbol(st.session_state.get(symbol_key, ""))
        expiry_key = f"{kind}_expiry_{symbol}"
        expirations: tuple[str, ...] = ()
        spot: float | None = None
        problem = ""
        if symbol and valid_symbol(symbol):
            try:
                expirations = fetch_expirations(symbol)
                spot = fetch_spot(symbol)
            except DataError as exc:
                problem = str(exc)
        elif symbol:
            problem = f"'{symbol}' doesn't look like a ticker symbol."

        # Suggest the strike nearest spot. The expiration widget doesn't exist
        # yet this run, so fall back to its last value (or the front month).
        # Until a symbol resolves there's nothing to suggest, so just prompt.
        hint = "Enter strike price"
        if expirations and spot:
            guess = st.session_state.get(expiry_key) or expirations[0]
            hint = f"{nearest_strike(symbol, guess, kind, spot) or spot:g}"

        cols = st.columns([1.4, 1, 1.6, 1], vertical_alignment="bottom")
        with cols[0]:
            st.text_input("Symbol", key=symbol_key, placeholder="Enter symbol")
        with cols[1]:
            strike = st.number_input("Strike", key=strike_key, min_value=0.01,
                                     step=0.50, format="%.2f", placeholder=hint)
        with cols[2]:
            expiration = st.selectbox(
                "Expiration", expirations or [None], key=expiry_key,
                format_func=lambda d: pretty_date(d) if d else "—",
                disabled=not expirations,
            )
        with cols[3]:
            submitted = st.button("＋ Add row", key=f"{kind}_add", type="primary",
                                  width="stretch", disabled=not expirations)

        if problem:
            st.caption(f":red[{problem}]")
        elif not expirations:
            st.caption("Expiration options load once a valid symbol is entered.")
        else:
            st.markdown(spot_line(symbol, expiration, kind, spot),
                        unsafe_allow_html=True)

    if submitted:
        if strike is None:
            st.warning("Enter a strike price first.")
            return
        with st.spinner(f"Looking up {symbol} {pretty_date(expiration)} {strike:g}…"):
            ok, message = add_row(kind, symbol, expiration, float(strike),
                                  min_oi, min_volume)
        if ok:
            st.session_state[f"{kind}_reset_strike"] = True
            st.rerun()
        st.warning(message)


# --------------------------------------------------------------------------- #
# UI — row cards
# --------------------------------------------------------------------------- #


def metric_cells(row: dict, kind: str) -> list[tuple[str, str, str, bool]]:
    """(label, value, tone class, full-width) for the metric grid."""
    q = row["quote"]
    money = lambda v: "—" if v is None else f"${v:,.2f}"  # noqa: E731

    if q["move"] >= 0:
        direction = "below" if kind == PUTS else "above"
        move = f"{money(q['move'])} ({abs(q['move_pct']):.1f}% {direction} spot)"
    else:
        move = f"already {money(-q['move'])} in the money"

    cells = [
        ("Bid / ask", f"{money(q['bid'])} / {money(q['ask'])}", "", False),
        ("Open interest / volume",
         f"{q['open_interest'] or 0:,.0f} / {q['volume'] or 0:,.0f}", "", False),
        ("Premium collected", f"${q['premium']:,.0f}",
         "good" if q["premium"] > 0 else "bad", False),
    ]
    if kind == PUTS:
        cells.append(("Cash to secure", f"${q['cash_required']:,.0f}", "", False))
        cells.append(("Drop needed for buyer to exercise", move, "", True))
    else:
        cells.append(("Requires", "100 shares owned", "", False))
        cells.append(("Rise needed for buyer to exercise", move, "", True))
    return cells


def commit_edit(row: dict, kind: str, min_oi: int, min_volume: int) -> None:
    """Re-price a row the moment its strike or expiration changes.

    Runs as a widget on_change callback, i.e. before the rerun, so the card is
    never drawn with values that don't match the inputs above them.
    """
    rid = row["id"]
    strike = st.session_state.get(f"strike_{rid}")
    if strike is None:  # box cleared; wait for a real number
        return
    row["strike"] = float(strike)
    row["expiration"] = st.session_state[f"expiry_{rid}"]
    reprice(row, kind, min_oi, min_volume)


def render_card(row: dict, kind: str, min_oi: int, min_volume: int) -> None:
    rid = row["id"]
    label = "put" if kind == PUTS else "call"
    strike_key, expiry_key = f"strike_{rid}", f"expiry_{rid}"

    # A snapped strike from the previous run is applied before the widget exists.
    pending = st.session_state.pop(f"pending_strike_{rid}", None)
    if pending is not None:
        st.session_state[strike_key] = float(pending)

    q, err = row["quote"], row["error"]

    with st.container(border=True):
        head, badge = st.columns([4, 1])
        with head:
            spot = f"spot ${q['spot']:,.2f}" if q else "no quote"
            st.markdown(
                f'<div class="oc-head">'
                f'<div class="oc-avatar">{row["symbol"][:5]}</div>'
                f'<div><div class="oc-title">{row["symbol"]} {label}</div>'
                f'<div class="oc-sub">{spot}</div></div></div>',
                unsafe_allow_html=True,
            )
        with badge:
            text, css = (q["badge"], q["badge_class"]) if q else ("Error", "bad")
            st.markdown(f'<div class="oc-badge-wrap">'
                        f'<span class="oc-badge {css}">{text}</span></div>',
                        unsafe_allow_html=True)

        st.divider()

        try:
            expirations = fetch_expirations(row["symbol"])
        except DataError:
            expirations = (row["expiration"],)
        if row["expiration"] not in expirations:
            expirations = (row["expiration"], *expirations)

        # Seed widget state rather than passing `value=`/`index=`, so that the
        # snapped-strike write-back above doesn't collide with a default.
        st.session_state.setdefault(strike_key, float(row["strike"]))
        st.session_state.setdefault(expiry_key, row["expiration"])

        # Editing either control re-prices the row on the spot.
        edit = {"on_change": commit_edit, "args": (row, kind, min_oi, min_volume)}

        controls = st.columns([1.1, 1.7, 3], vertical_alignment="bottom")
        with controls[0]:
            st.number_input("Strike", key=strike_key, min_value=0.01,
                            step=0.50, format="%.2f", **edit)
        with controls[1]:
            st.selectbox("Expiration", expirations, key=expiry_key,
                         format_func=pretty_date, **edit)

        st.divider()

        if err:
            st.error(err)
        else:
            cells = "".join(
                f'<div class="oc-cell{" oc-wide" if wide else ""}">'
                f'<div class="oc-lbl">{lbl}</div>'
                f'<div class="oc-val {tone}">{val}</div></div>'
                for lbl, val, tone, wide in metric_cells(row, kind)
            )
            st.markdown(f'<div class="oc-metrics">{cells}</div>',
                        unsafe_allow_html=True)

            notes = []
            if q["note"]:
                notes.append(q["note"])
            if abs(q["strike"] - q["requested_strike"]) > 1e-9:
                notes.append(f"Nearest listed strike to "
                             f"{q['requested_strike']:g} was {q['strike']:g}.")
            notes.append(f"Quoted {q['fetched_at']:%H:%M:%S}.")
            st.caption(" ".join(notes))

        _, remove_col = st.columns([4, 1])
        with remove_col:
            remove = st.button("🗑 Remove", key=f"remove_{rid}", width="stretch")

    if remove:
        st.session_state[kind] = [r for r in st.session_state[kind] if r["id"] != rid]
        st.rerun()


def render_section(kind: str, min_oi: int, min_volume: int) -> None:
    count = len(st.session_state[kind])
    if kind == PUTS:
        st.header(f"Puts · {count}", anchor="puts", divider="gray")
        st.caption("Cash-secured put: you set aside strike × 100 in cash and get "
                   "assigned if the stock closes below the strike.")
    else:
        st.header(f"Calls · {count}", anchor="calls", divider="gray")
        st.caption("Covered call: assumes you already own 100 shares per contract; "
                   "they get called away if the stock closes above the strike.")

    render_add_form(kind, min_oi, min_volume)

    rows = st.session_state[kind]
    if not rows:
        st.info("No rows yet. Add one above.")
        return
    for row in list(rows):
        render_card(row, kind, min_oi, min_volume)


def main() -> None:
    st.set_page_config(page_title="Options Selling Explorer", page_icon="📈",
                       layout="wide")
    init_state()
    st.markdown(CSS, unsafe_allow_html=True)

    st.title("📈 Options Selling Explorer")
    st.caption("Live quotes from Yahoo Finance via yfinance. Paper exploration "
               "only — nothing here places a trade.")

    with st.sidebar:
        st.header("Liquidity thresholds")
        min_oi = st.number_input("Minimum open interest", min_value=0, value=100, step=25)
        min_volume = st.number_input("Minimum daily volume", min_value=0, value=10, step=5)
        st.caption("Rows below either threshold are badged **Thin**. A missing or "
                   "zero bid is always badged **No bid**.")

    header, refresh_col = st.columns([3, 1])
    with header:
        st.write(f"Last refreshed **{st.session_state.last_refresh:%H:%M:%S}** · "
                 f"{len(st.session_state[PUTS])} put(s), "
                 f"{len(st.session_state[CALLS])} call(s)")
    with refresh_col:
        refresh = st.button("🔄 Refresh all", width="stretch")

    if refresh:
        st.cache_data.clear()
        with st.spinner("Re-pulling every row…"):
            for kind in (PUTS, CALLS):
                for row in st.session_state[kind]:
                    reprice(row, kind, min_oi, min_volume)
        st.session_state.last_refresh = datetime.now()
        st.rerun()

    # One scrollable page: puts on top, calls underneath.
    render_section(PUTS, min_oi, min_volume)
    st.write("")
    render_section(CALLS, min_oi, min_volume)


if __name__ == "__main__":
    main()
