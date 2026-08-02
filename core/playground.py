"""
The paper-trading playground tab.

Owns the settlement sweep and all of the playground's rendering. `explorer.py`
calls `sell_gate`/`sell_from_row` for the Sell buttons on its cards, so every
policy decision about paper trading lives here.

The Store itself is passed in rather than fetched: the public app hands over a
per-visitor in-memory book and the local app a shared one on disk. See
`core.stores`.
"""

from __future__ import annotations

import time as _time
from datetime import date

import pandas as pd
import streamlit as st

from . import market, paper
from .market import CALLS, PUTS, DataError, pretty_date

# Don't re-sweep on every rerun; expired positions don't get more expired.
SWEEP_COOLDOWN = 60.0

# How often the live section re-prices itself. Must stay LONGER than
# market.SPOT_TTL so that every tick finds the spot cache expired and fetches a
# genuinely new price — if the two were equal, a tick could land on a
# still-valid entry and the price would look frozen for two cycles.
# Because the tabs are lazy, this only ticks while the Playground is on screen.
LIVE_REFRESH_SECONDS = 20

CASH_SETTLED_NOTE = (
    "Paper trading only — nothing here places a real trade. Positions settle in cash at "
    "the option's intrinsic value on the expiration date; no shares change hands. The "
    "reserve is a paper risk budget, not share ownership, so there is no share P&L."
)


def md(text: str) -> str:
    """Escape dollar signs for any string rendered as markdown.

    Streamlit's markdown treats `$...$` as inline LaTeX, so a caption holding two
    money amounts silently typesets everything between them as an equation.
    Anything going into st.caption/warning/error/toast or a `help` tooltip has to
    come through here.

    The explorer's cards don't need this: their numbers live inside <div> blocks,
    which CommonMark hands through as raw HTML without parsing inline markdown.
    """
    return text.replace("$", r"\$")


def usd(value: float) -> str:
    """Money formatted for a markdown context — see `md`."""
    return f"\\${value:,.2f}"


# --------------------------------------------------------------------------- #
# Settlement
# --------------------------------------------------------------------------- #


def due_count(store: paper.Store) -> int:
    """How many open positions are past their expiration close.

    Pure arithmetic over the in-memory book — no network — so app.py can call it
    on every render to badge the tab.
    """
    return len(store.due_for_settlement(market.market_close_passed))


def settle_due_positions(store: paper.Store, *, force: bool = False) -> list[str]:
    """Settle every position whose expiration close has passed.

    Runs when the playground is on screen rather than at app load: it's network
    I/O proportional to the number of expired symbols, and putting it on load
    would tax every explorer keystroke for output nobody is looking at. The
    trade-off is that a position doesn't settle until you visit this tab, so its
    reserve stays locked until then — conservative in the safe direction.
    """
    if store.read_only:
        return []
    if not force and (_time.monotonic() - store.last_sweep_at) < SWEEP_COOLDOWN:
        return []

    due = store.due_for_settlement(market.market_close_passed)
    store.last_sweep_at = _time.monotonic()
    if not due:
        return []

    messages: list[str] = []
    with st.spinner(f"Settling {len(due)} expired position(s)…"):
        for pos in due:
            try:
                close, price_date, note = market.fetch_settlement_close(
                    pos["symbol"], pos["expiration"])
            except DataError as exc:
                store.mutate(paper.mark_pending, pos["id"], str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - never blank the page
                store.mutate(paper.mark_pending, pos["id"], f"Unexpected error: {exc}")
                continue
            ok, message = store.mutate(paper.settle_position, pos["id"], close,
                                       price_date, note)
            if ok:
                messages.append(message)
    return messages


# --------------------------------------------------------------------------- #
# Selling (called from the explorer tab)
# --------------------------------------------------------------------------- #


def sell_gate(row: dict, kind: str, store: paper.Store) -> tuple[bool, str]:
    """Can this explorer row be sold, and if not, why?

    The caller disables the button and shows the reason as its tooltip rather
    than hiding it — a greyed button that explains itself teaches the accounting
    model, a missing button is a mystery. `paper.sell_position` re-checks all of
    this; that check is correctness, this one is affordance.
    """
    if store.read_only:
        return False, "The saved playground is read-only (written by a newer version)."

    quote = row.get("quote")
    if quote is None:
        return False, "No quote for this row yet, so there's nothing to sell."
    if not quote.get("bid"):
        return False, "No live bid, so there's no premium to collect."
    if market.market_close_passed(row["expiration"]):
        return False, "This contract has already expired."

    reserve = paper.reserve_for(kind, quote["strike"], quote["spot"])
    available = paper.available_cash(store.book)
    if available < reserve:
        # This lands in a button tooltip, which is markdown — hence usd().
        needed = "the strike" if kind == PUTS else "100 shares at spot"
        return False, (f"Needs {usd(reserve)} available ({needed} × 100); you have "
                       f"{usd(available)}.")

    return True, ("Paper trade only — records this position on the Playground tab. "
                  "No order is placed.")


def close_quotes(positions: list[dict]) -> dict[int, float | None]:
    """Current ask per position id — None wherever the contract can't be priced.

    One cached chain fetch per distinct (symbol, expiration, kind), shared by
    every position on it. Never raises: a dead ticker shows as unpriced rather
    than blanking the page.
    """
    asks: dict[int, float | None] = {}
    for pos in positions:
        try:
            asks[pos["id"]] = market.fetch_ask(pos["symbol"], pos["expiration"],
                                               pos["strike"], pos["kind"])
        except DataError:
            asks[pos["id"]] = None
        except Exception:  # noqa: BLE001 - a bad ticker must not blank the page
            asks[pos["id"]] = None
    return asks


def matching_positions(book: dict, symbol: str, kind: str, expiration: str,
                       strike: float) -> tuple[list[dict], list[dict]]:
    """Paper positions for exactly this contract, as (open, closed).

    Matched on the contract, not on the explorer row that spawned it — a row is
    only a view. Editing its strike makes it a different contract and it stops
    matching, which is right; removing the row doesn't touch the position.
    """
    def same(rec: dict) -> bool:
        return (rec.get("symbol") == symbol
                and rec.get("kind") == kind
                and rec.get("expiration") == expiration
                and abs(float(rec.get("strike", 0.0)) - float(strike)) < 1e-9)

    return ([p for p in book["open"] if same(p)],
            [h for h in book["history"] if same(h)])


def sold_note(book: dict, symbol: str, kind: str, expiration: str,
              strike: float) -> str:
    """One line telling an explorer card what the playground already knows about
    this contract. Empty when it's never been sold."""
    open_pos, closed_pos = matching_positions(book, symbol, kind, expiration, strike)
    if open_pos:
        premium = sum(p["premium_collected"] for p in open_pos)
        held = "position" if len(open_pos) == 1 else "positions"
        return (f":green[:material/check_circle: On the Playground — {len(open_pos)} open "
                f"{held}, {usd(premium)} premium collected.]")
    if closed_pos:
        realized = sum(h.get("realized_pnl", 0.0) for h in closed_pos)
        times = "once" if len(closed_pos) == 1 else f"{len(closed_pos)} times"
        return (f":gray[:material/history: Sold {times} before and closed — realized "
                f"{usd(realized)}.]")
    return ""


def sell_from_row(store: paper.Store, row: dict, kind: str) -> tuple[bool, str]:
    """Sell the contract exactly as the card is showing it.

    Uses `row["quote"]` as-is with no fresh fetch, so what you saw is what gets
    booked — including the *snapped* strike. The quote can be up to a minute old
    from the cache, so its timestamp goes into the position for the record.
    """
    quote = row.get("quote")
    if quote is None:
        return False, "No quote for this row yet, so there's nothing to sell."

    fetched_at = quote.get("fetched_at")
    return store.mutate(
        paper.sell_position,
        symbol=row["symbol"],
        kind=kind,
        expiration=row["expiration"],
        strike=quote["strike"],
        premium_per_share=quote["bid"],
        spot_at_sale=quote["spot"],
        quote_fetched_at=fetched_at.isoformat(timespec="seconds") if fetched_at else None,
    )


# --------------------------------------------------------------------------- #
# Dialogs
# --------------------------------------------------------------------------- #


@st.dialog("Buy to close", icon=":material/shopping_cart:")
def close_dialog(store: paper.Store, position_id: int, min_oi: int, min_volume: int) -> None:
    """Price the contract and buy it back.

    The ask is fetched here, on click, rather than for every open position on
    every render — that would be one chain fetch per position per rerun.
    """
    pos = paper.find_open(store.book, position_id)
    if pos is None:
        st.error("That position is no longer open — it may have just settled.")
        return

    st.write(f"**{paper.describe(pos)}**")
    try:
        quote = market.price_contract(pos["symbol"], pos["expiration"], pos["strike"],
                                      pos["kind"], min_oi, min_volume)
    except DataError as exc:
        st.error(md(f"Could not price this contract right now: {exc}"))
        return

    ask = quote["ask"]
    if ask is None:
        st.error("No ask is quoted for this contract, so it can't be priced to close.")
        return

    cost = ask * paper.SHARES_PER_CONTRACT * pos.get("contracts", 1)
    net = pos["premium_collected"] - cost

    cols = st.columns(3)
    cols[0].metric("Ask", f"${ask:,.2f}")
    cols[1].metric("Cost to close", f"${cost:,.2f}")
    cols[2].metric("Realized P&L", f"${net:,.2f}", delta=round(net, 2))

    st.caption(f"You collected {usd(pos['premium_collected'])} selling this. Buying it "
               f"back at the ask releases the {usd(pos['reserved'])} reserve.")
    if ask == 0:
        st.info(f"The ask is {usd(0)} — this contract is worthless, so closing costs "
                "nothing.", icon=":material/info:")

    with st.container(horizontal=True, horizontal_alignment="right"):
        if st.button("Cancel", key="close_cancel"):
            st.rerun()
        if st.button("Buy to close", key="close_confirm", type="primary",
                     icon=":material/check:"):
            ok, message = store.mutate(paper.buy_to_close, position_id, ask)
            st.toast(md(message), icon=":material/check:" if ok else ":material/error:")
            st.rerun()


@st.dialog("Abandon position", icon=":material/block:")
def abandon_dialog(store: paper.Store, position_id: int) -> None:
    pos = paper.find_open(store.book, position_id)
    if pos is None:
        st.error("That position is no longer open.")
        return

    st.write(f"**{paper.describe(pos)}**")
    st.warning("Abandoning writes this to history with no settlement cost and releases "
               "its reserve. Use it when a settlement price will never arrive — a "
               "delisted ticker, say — otherwise the reserve stays locked forever.",
               icon=":material/warning:")
    if pos.get("settle_error"):
        st.caption(md(f"Last error: {pos['settle_error']}"))

    with st.container(horizontal=True, horizontal_alignment="right"):
        if st.button("Cancel", key="abandon_cancel"):
            st.rerun()
        if st.button("Abandon", key="abandon_confirm", type="primary",
                     icon=":material/block:"):
            ok, message = store.mutate(paper.abandon_position, position_id,
                                       pos.get("settle_error") or "")
            st.toast(md(message), icon=":material/check:" if ok else ":material/error:")
            st.rerun()


@st.dialog("Reset playground", icon=":material/restart_alt:")
def reset_dialog(store: paper.Store) -> None:
    book = store.book
    st.warning(f"This deletes {len(book['open'])} open position(s) and "
               f"{len(book['history'])} history record(s), and cannot be undone.",
               icon=":material/warning:")
    balance = st.number_input("Starting balance", min_value=1_000.0, max_value=10_000_000.0,
                              step=1_000.0, format="%.2f",
                              value=float(book["starting_balance"]),
                              help="The cash the fresh playground starts with.")
    with st.container(horizontal=True, horizontal_alignment="right"):
        if st.button("Cancel", key="reset_cancel"):
            st.rerun()
        if st.button("Reset playground", key="reset_confirm", type="primary",
                     icon=":material/restart_alt:"):
            ok, message = store.mutate(paper.reset_book, float(balance))
            st.toast(md(message), icon=":material/check:" if ok else ":material/error:")
            st.rerun()


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render_balance_header(store: paper.Store, totals: dict) -> None:
    cols = st.columns(4)
    with cols[0]:
        st.metric("Cash balance", f"${totals['cash']:,.2f}",
                  delta=round(totals["cash"] - totals["starting_balance"], 2),
                  delta_description="vs starting balance", border=True)
    with cols[1]:
        st.metric("Available cash", f"${totals['available']:,.2f}", border=True,
                  help="Cash balance minus reserved collateral. This is what new "
                       "sales are checked against.")
    with cols[2]:
        st.metric("Unrealized liability", f"${totals['unrealized']:,.2f}", border=True,
                  help="What you would owe if every open position settled at today's "
                       "price.")
    with cols[3]:
        st.metric("Realized P&L", f"${totals['realized']:,.2f}",
                  delta=round(totals["realized"], 2), border=True,
                  help="Premiums kept, minus what settled or closed positions cost.")

    detail = (f"Reserved **{usd(totals['reserved'])}** across {totals['open_count']} open "
              f"position(s) · starting balance {usd(totals['starting_balance'])} · "
              f"equity (cash − unrealized) **{usd(totals['equity'])}**")
    if totals["missing_prices"]:
        detail += (f" · unrealized excludes {', '.join(totals['missing_prices'])} "
                   f"(no price available)")
    st.caption(detail)

    if totals["available"] < 0:
        st.error(f"Available cash is {usd(totals['available'])}. A short call's loss "
                 "isn't capped by its reserve, so a big move against you can overdraw "
                 "the account. You can't open new positions until this is back above "
                 "zero.", icon=":material/error:")


def _position_card(store: paper.Store, pos: dict, spot: float | None,
                   ask: float | None, min_oi: int, min_volume: int) -> None:
    kind_label = "put" if pos["kind"] == PUTS else "call"
    contracts = pos.get("contracts", 1)
    pending = pos.get("status") == paper.PENDING

    try:
        days_left = (date.fromisoformat(pos["expiration"]) - date.today()).days
    except ValueError:
        days_left = None

    with st.container(border=True):
        head, badge = st.columns([4, 1], vertical_alignment="center")
        with head:
            st.markdown(f"**{pos['symbol']} {pos['strike']:g} {kind_label}** · "
                        f"{pretty_date(pos['expiration'])}")
            if spot is None:
                sub = "spot unavailable"
            else:
                sub = f"spot {usd(spot)}"
            if days_left is not None:
                sub += (f" · expires today" if days_left == 0 else
                        f" · {days_left}d left" if days_left > 0 else
                        f" · expired {abs(days_left)}d ago")
            st.caption(sub)
        with badge:
            if pending:
                st.badge("Awaiting price", color="orange", icon=":material/schedule:")
            elif spot is not None and paper.intrinsic(pos["kind"], pos["strike"], spot,
                                                      contracts) > 0:
                st.badge("In the money", color="red", icon=":material/trending_down:")
            else:
                st.badge("Out of the money", color="green", icon=":material/check:")

        premium = pos["premium_collected"]
        cost = None if ask is None else ask * paper.SHARES_PER_CONTRACT * contracts
        close_pnl = None if cost is None else premium - cost
        owed = (paper.intrinsic(pos["kind"], pos["strike"], spot, contracts)
                if spot is not None else None)
        settle_pnl = None if owed is None else premium - owed

        def pnl_metric(col, label: str, value: float | None, help_text: str) -> None:
            if value is None:
                col.metric(label, "—", help=help_text)
                return
            # A string delta colours itself from a leading minus, so this shows
            # how much of the premium you'd keep and turns red once you'd lose.
            kept = (value / premium * 100) if premium else 0.0
            col.metric(label, f"${value:,.2f}", delta=f"{kept:.0f}% of premium",
                       help=help_text)

        cells = st.columns(4)
        cells[0].metric("Premium collected", f"${premium:,.2f}",
                        help="Cash credited when you sold, and the most this position "
                             "can ever make.")
        if spot is None:
            cells[1].metric("Spot price", "—", help="No price available right now.")
        else:
            # For a short put a rising stock is good news; for a short call it's
            # the opposite, so the arrow's colour has to flip.
            cells[1].metric("Spot price", f"${spot:,.2f}",
                            delta=round(spot - pos["spot_at_sale"], 2),
                            delta_description="since you sold",
                            delta_color="normal" if pos["kind"] == PUTS else "inverse",
                            help=f"Strike is ${pos['strike']:,.2f}.")
        pnl_metric(cells[2], "P&L if settled now", settle_pnl,
                   "What you'd net if the contract expired at today's spot price: "
                   "premium collected minus the intrinsic value you'd owe. Ignores "
                   "the time value still left on the contract.")
        pnl_metric(cells[3], "P&L if closed now", close_pnl,
                   "What you'd net buying the contract back at its current ask. This "
                   "is the one you can act on today, via Buy to close.")

        bits = [f"Reserved {usd(pos['reserved'])}"]
        if cost is not None:
            bits.append(f"costs {usd(cost)} to buy back (ask {usd(ask)})")
        if owed is not None:
            bits.append(f"owed if settled now {usd(owed)}")
        bits.append(f"sold {pos.get('sold_at', '')[:16].replace('T', ' ')} at "
                    f"{usd(pos['premium_per_share'])}/share with the stock at "
                    f"{usd(pos['spot_at_sale'])}")
        st.caption(" · ".join(bits) + ".")

        if pending and pos.get("settle_error"):
            st.caption(f":orange[{md(pos['settle_error'])}]")

        with st.container(horizontal=True, horizontal_alignment="right"):
            if pending:
                if st.button("Retry settlement", key=f"pg_retry_{pos['id']}",
                             icon=":material/refresh:"):
                    st.session_state["pg_force_sweep"] = True
                    st.rerun()
                if st.button("Abandon", key=f"pg_abandon_{pos['id']}",
                             icon=":material/block:"):
                    abandon_dialog(store, pos["id"])
            if st.button("Buy to close", key=f"pg_close_{pos['id']}", type="primary",
                         icon=":material/shopping_cart:", disabled=store.read_only,
                         help="Pay the current ask to exit before expiry."):
                close_dialog(store, pos["id"], min_oi, min_volume)


def render_open_positions(store: paper.Store, spots: dict[str, float | None],
                          min_oi: int, min_volume: int) -> None:
    positions = store.book["open"]
    st.subheader(f"Open positions · {len(positions)}", divider="gray")

    if not positions:
        st.info("No open positions yet. Add a row on the Explorer tab, then use its "
                "Sell button.", icon=":material/info:")
        return

    asks = close_quotes(positions)
    st.caption(f"Live while this tab is open · spot every {LIVE_REFRESH_SECONDS}s, "
               f"close prices every {market.CHAIN_TTL}s · last checked "
               f"{market.now_et():%H:%M:%S} ET · quotes are delayed by the provider")

    stuck = [p for p in positions if p.get("status") == paper.PENDING]
    if stuck:
        st.warning(md(f"{len(stuck)} position(s) have expired but couldn't be priced yet. "
                      f"Their reserves stay locked until they settle. "
                      f"First error: {stuck[0].get('settle_error', 'unknown')}"),
                   icon=":material/schedule:")

    # Soonest expiration first — that's the one that needs attention.
    for pos in sorted(positions, key=lambda p: (p["expiration"], p["symbol"])):
        _position_card(store, pos, spots.get(pos["symbol"]), asks.get(pos["id"]),
                       min_oi, min_volume)


def _history_frame(history: list[dict]) -> pd.DataFrame:
    rows = []
    for rec in reversed(history):  # newest first
        rows.append({
            "Symbol": rec.get("symbol", ""),
            "Type": "Put" if rec.get("kind") == PUTS else "Call",
            "Strike": float(rec.get("strike", 0.0)),
            "Expiration": rec.get("expiration"),
            "Premium": rec.get("premium_collected", 0.0),
            "Outcome": paper.OUTCOME_LABELS.get(rec.get("status", ""), rec.get("status", "")),
            "Settled at": rec.get("settlement_price"),
            "Cost": rec.get("cost_to_close", 0.0),
            "Realized P&L": rec.get("realized_pnl", 0.0),
            "Closed": (rec.get("closed_at") or "")[:10],
            "Note": rec.get("settlement_note") or "",
        })
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame["Expiration"] = pd.to_datetime(frame["Expiration"], errors="coerce")
        frame["Closed"] = pd.to_datetime(frame["Closed"], errors="coerce")
    return frame


def render_history(store: paper.Store) -> None:
    history = store.book["history"]
    st.subheader(f"History · {len(history)}", divider="gray")

    if not history:
        st.info("Nothing has settled or been closed yet. Positions land here at "
                "expiration, or when you buy one back.", icon=":material/info:")
        return

    exercised = sum(1 for h in history if h.get("status") == paper.EXERCISED)
    total = paper.realized_pnl(store.book)
    st.caption(f"{len(history)} closed · {exercised} exercised · realized "
               f"**{usd(total)}**")

    st.dataframe(
        _history_frame(history),
        hide_index=True,
        column_config={
            "Strike": st.column_config.NumberColumn(format="$%.2f"),
            "Expiration": st.column_config.DateColumn(format="MMM DD, YYYY"),
            "Premium": st.column_config.NumberColumn(format="$%.2f",
                                                     help="What you collected selling it."),
            "Settled at": st.column_config.NumberColumn(
                format="$%.2f", help="The underlying's close on the expiration date."),
            "Cost": st.column_config.NumberColumn(
                format="$%.2f", help="Paid to the buyer at exercise, or to buy the "
                                     "contract back early."),
            "Realized P&L": st.column_config.NumberColumn(format="$%.2f"),
            "Closed": st.column_config.DateColumn(format="MMM DD, YYYY"),
        },
    )


@st.fragment(run_every=LIVE_REFRESH_SECONDS)
def _live_section(store: paper.Store, min_oi: int, min_volume: int) -> None:
    """Everything that goes stale on its own, re-rendered on a timer.

    Open positions are marked against the live ask, and the unrealized liability
    against the live spot, so both drift as the market moves even when nobody
    touches the page. A fragment reruns just this block rather than the whole
    app, so the explorer's widget state is untouched.

    Kept sequential (not `parallel=True`) because that mode forbids st.dialog,
    which the buy-to-close and abandon flows need.
    """
    if store.save_error:
        st.error(md(store.save_error), icon=":material/save:")

    drift = store.invariant_warning()
    if drift:
        st.warning(md(f"The saved balance doesn't add up. {drift} Nothing has been "
                      "changed automatically — reset the playground to start clean."),
                   icon=":material/calculate:")

    forced = bool(st.session_state.pop("pg_force_sweep", False))
    for message in settle_due_positions(store, force=forced):
        st.toast(md(message), icon=":material/gavel:")

    spots = market.spots_for(p["symbol"] for p in store.book["open"])
    render_balance_header(store, paper.summary(store.book, spots))
    st.write("")
    render_open_positions(store, spots, min_oi, min_volume)
    st.write("")
    render_history(store)


def render_playground(store: paper.Store, min_oi: int, min_volume: int) -> None:
    st.header("Paper trading playground", anchor="playground", divider="gray")
    st.caption(CASH_SETTLED_NOTE)

    if store.read_only:
        st.error(md(store.load_warning or "This playground is read-only."),
                 icon=":material/lock:")
    elif store.load_warning:
        st.warning(md(store.load_warning), icon=":material/warning:")

    # Outside the fragment: resetting is a deliberate act, not something that
    # needs re-rendering on a timer.
    with st.container(horizontal=True, horizontal_alignment="right"):
        if st.button("Reset playground", icon=":material/restart_alt:",
                     disabled=store.read_only,
                     help="Clear every position and start again from a fresh balance."):
            reset_dialog(store)

    _live_section(store, min_oi, min_volume)
