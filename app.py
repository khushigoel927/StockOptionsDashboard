"""
Options Selling Explorer — a live dashboard for eyeballing cash-secured puts
and covered calls, plus a paper-trading playground for practising selling them.

The Explorer tab is not a position tracker and not connected to a broker: rows
are just "what would this contract look like right now" scratch cards that live
in st.session_state. The Playground tab *does* keep score, but only on paper —
see playground.py.

Run with:  streamlit run app.py   (or ./app.sh start)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import streamlit as st

import market
import paper
import playground
from market import (CALLS, PUTS, DataError, fetch_expirations, fetch_quotable_strikes,
                    fetch_spot, fetch_strikes, normalize_symbol, pretty_date,
                    price_contract, valid_symbol)

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


@dataclass
class Ctx:
    """What every explorer renderer needs: the liquidity thresholds and the book."""
    store: paper.Store
    min_oi: int
    min_volume: int


# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #


def init_state() -> None:
    st.session_state.setdefault(PUTS, [])
    st.session_state.setdefault(CALLS, [])
    st.session_state.setdefault("next_id", 1)
    st.session_state.setdefault("last_refresh", datetime.now())


def reprice(row: dict, kind: str, ctx: Ctx) -> None:
    """Refresh a row in place from its committed strike/expiration."""
    try:
        row["quote"] = price_contract(row["symbol"], row["expiration"], row["strike"],
                                      kind, ctx.min_oi, ctx.min_volume)
        row["error"] = None
        # Snap the committed strike to the contract we actually found.
        row["strike"] = row["quote"]["strike"]
        st.session_state[f"pending_strike_{row['id']}"] = row["strike"]
    except DataError as exc:
        row["quote"] = None
        row["error"] = str(exc)


def add_row(kind: str, symbol: str, expiration: str, strike: float,
            ctx: Ctx) -> tuple[bool, str]:
    rows = st.session_state[kind]
    if any(r["symbol"] == symbol and r["expiration"] == expiration
           and abs(r["strike"] - strike) < 1e-9 for r in rows):
        return False, f"{symbol} {pretty_date(expiration)} {strike:g} is already listed."

    row = {"id": st.session_state.next_id, "symbol": symbol,
           "expiration": expiration, "strike": float(strike),
           "quote": None, "error": None}
    st.session_state.next_id += 1
    reprice(row, kind, ctx)
    rows.append(row)
    return True, f"Added {symbol} {pretty_date(expiration)} {strike:g}."


# --------------------------------------------------------------------------- #
# UI — add form
# --------------------------------------------------------------------------- #


def nearest_strike(symbol: str, expiration: str, kind: str,
                   spot: float) -> float | None:
    """Closest strike to spot that has a live bid; None if the chain can't be read.

    Prefers quotable strikes so the suggested strike is one you could actually
    sell, falling back to the full list when nothing is quoted.
    """
    try:
        strikes = (fetch_quotable_strikes(symbol, expiration, kind)
                   or fetch_strikes(symbol, expiration, kind))
    except DataError:
        return None
    return min(strikes, key=lambda s: abs(s - spot)) if strikes else None


def spot_line(symbol: str, expiration: str, kind: str, spot: float | None) -> str:
    """The 'here's what the stock costs right now' line under the add form.

    Reports the range of strikes with a live bid rather than every listed
    strike: deep out-of-the-money strikes stay listed long after anyone stops
    quoting them, so the listed range suggests contracts you can't sell.
    """
    price = f"<b>${spot:,.2f}</b>" if spot else "<b>unavailable</b>"
    meta = ""
    try:
        listed = fetch_strikes(symbol, expiration, kind)
        quotable = fetch_quotable_strikes(symbol, expiration, kind)
        if quotable:
            meta = (f" · {len(quotable)} of {len(listed)} strikes have a live bid for "
                    f"{pretty_date(expiration)} "
                    f"(${min(quotable):,.2f} – ${max(quotable):,.2f})")
            if spot:
                near = min(quotable, key=lambda s: abs(s - spot))
                meta += f" · nearest to spot <b>{near:g}</b>"
        elif listed:
            meta = (f" · none of the {len(listed)} strikes listed for "
                    f"{pretty_date(expiration)} have a live bid right now")
    except DataError:
        pass
    return (f'<div class="oc-spot"><span class="oc-spot-tick">{symbol}</span> '
            f'spot {price}<span class="oc-spot-meta">{meta}</span></div>')


def render_add_form(kind: str, ctx: Ctx) -> None:
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
        # persist_state keeps what you typed when you switch to the Playground
        # tab and back — without it Streamlit drops the value of any widget that
        # didn't render, and the lazy tabs mean this one doesn't.
        with cols[0]:
            st.text_input("Symbol", key=symbol_key, placeholder="Enter symbol",
                          persist_state="session")
        with cols[1]:
            strike = st.number_input("Strike", key=strike_key, min_value=0.01,
                                     step=0.50, format="%.2f", placeholder=hint,
                                     persist_state="session")
        with cols[2]:
            expiration = st.selectbox(
                "Expiration", expirations or [None], key=expiry_key,
                format_func=lambda d: pretty_date(d) if d else "—",
                disabled=not expirations, persist_state="session",
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
            ok, message = add_row(kind, symbol, expiration, float(strike), ctx)
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


def commit_edit(row: dict, kind: str, ctx: Ctx) -> None:
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
    reprice(row, kind, ctx)


def render_card(row: dict, kind: str, ctx: Ctx) -> None:
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
        edit = {"on_change": commit_edit, "args": (row, kind, ctx)}

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

        # Selling copies the quote into the playground book; it never holds a
        # reference to this row, so removing the card leaves the position alone.
        # Selling the same contract twice is allowed on purpose — that's two
        # real contracts, even though the explorer rejects duplicate *rows*.
        can_sell, why = playground.sell_gate(row, kind, ctx.store)
        sold = playground.sold_note(ctx.store.book, row["symbol"], kind,
                                    row["expiration"], row["strike"])
        note_col, button_col = st.columns([2, 2], vertical_alignment="center")
        with note_col:
            if sold:
                st.caption(sold)
        with button_col:
            with st.container(horizontal=True, horizontal_alignment="right"):
                # "Sell another" makes it obvious the first one landed, rather
                # than leaving you wondering whether the click registered.
                verb = "Sell another" if sold.startswith(":green") else "Sell"
                sell = st.button(f"{verb} {label}", key=f"sell_{rid}", type="primary",
                                 icon=":material/sell:", disabled=not can_sell, help=why)
                remove = st.button("Remove", key=f"remove_{rid}",
                                   icon=":material/delete:")

    if sell:
        ok, message = playground.sell_from_row(ctx.store, row, kind)
        # Toasts render as markdown, so the dollar signs need escaping or two of
        # them turn the text between into LaTeX.
        st.toast(playground.md(("Paper: " + message) if ok else message),
                 icon=":material/sell:" if ok else ":material/error:")
        if ok:
            st.rerun()

    if remove:
        st.session_state[kind] = [r for r in st.session_state[kind] if r["id"] != rid]
        st.rerun()


def render_section(kind: str, ctx: Ctx) -> None:
    count = len(st.session_state[kind])
    if kind == PUTS:
        st.header(f"Puts · {count}", anchor="puts", divider="gray")
        st.caption("Cash-secured put: you set aside strike × 100 in cash and get "
                   "assigned if the stock closes below the strike.")
    else:
        st.header(f"Calls · {count}", anchor="calls", divider="gray")
        st.caption("Covered call: assumes you already own 100 shares per contract; "
                   "they get called away if the stock closes above the strike.")

    render_add_form(kind, ctx)

    rows = st.session_state[kind]
    if not rows:
        st.info("No rows yet. Add one above.")
        return
    for row in list(rows):
        render_card(row, kind, ctx)


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


def render_toolbar(ctx: Ctx) -> None:
    header, refresh_col = st.columns([3, 1], vertical_alignment="center")
    with header:
        st.write(f"Last refreshed **{st.session_state.last_refresh:%H:%M:%S}** · "
                 f"{len(st.session_state[PUTS])} put(s), "
                 f"{len(st.session_state[CALLS])} call(s) on the Explorer · "
                 f"{len(ctx.store.book['open'])} open paper position(s)")
    with refresh_col:
        refresh = st.button("Refresh all", width="stretch", icon=":material/refresh:")

    # Positions only settle while the Playground is on screen, so say when some
    # are waiting rather than leaving their reserves quietly locked.
    due = playground.due_count(ctx.store)
    if due:
        st.caption(f":orange[{due} paper position(s) have expired and are ready to "
                   f"settle — open the Playground tab.]")

    if refresh:
        # Not st.cache_data.clear(): that would also drop settled prices, which
        # never change and are expensive to re-fetch.
        market.clear_quote_caches()
        st.session_state["pg_force_sweep"] = True
        with st.spinner("Re-pulling every row…"):
            for kind in (PUTS, CALLS):
                for row in st.session_state[kind]:
                    reprice(row, kind, ctx)
        st.session_state.last_refresh = datetime.now()
        st.rerun()


def main() -> None:
    st.set_page_config(page_title="Options Selling Explorer", page_icon="📈",
                       layout="wide")
    init_state()
    st.markdown(CSS, unsafe_allow_html=True)

    st.title("📈 Options Selling Explorer")
    st.caption("Live quotes from Yahoo Finance via yfinance. Paper trading only — the "
               "Playground tab simulates positions you sell here, and nothing in this "
               "app places a real trade.")

    with st.sidebar:
        st.header("Liquidity thresholds")
        min_oi = st.number_input("Minimum open interest", min_value=0, value=100, step=25)
        min_volume = st.number_input("Minimum daily volume", min_value=0, value=10, step=5)
        st.caption("Rows below either threshold are badged **Thin**. A missing or "
                   "zero bid is always badged **No bid**.")

    ctx = Ctx(store=playground.get_store(), min_oi=int(min_oi), min_volume=int(min_volume))
    render_toolbar(ctx)

    # on_change="rerun" is what makes `.open` meaningful — without it Streamlit
    # runs *both* tab bodies every time, so the playground's price fetches would
    # fire on every keystroke in the explorer.
    explorer_tab, playground_tab = st.tabs(["Explorer", "Playground"],
                                           on_change="rerun", key="main_tab")

    if explorer_tab.open:
        with explorer_tab:
            render_section(PUTS, ctx)
            st.write("")
            render_section(CALLS, ctx)

    if playground_tab.open:
        with playground_tab:
            playground.render_playground(ctx.store, ctx.min_oi, ctx.min_volume)


if __name__ == "__main__":
    main()
