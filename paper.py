"""
The paper-trading book: schema, persistence, and all of the accounting.

Deliberately imports nothing from streamlit or yfinance. A sign error in this
module would quietly corrupt someone's balance, so it has to be drivable from a
plain REPL:

    from pathlib import Path
    import paper
    book = paper.new_book()
    paper.sell_position(book, symbol="AAPL", kind="puts", expiration="2026-08-21",
                        strike=320.0, premium_per_share=4.25, spot_at_sale=336.23)
    assert paper.check_invariant(book) is None

Model, in one paragraph: selling an option credits the premium to `cash` right
away and locks a `reserve` against the position (a lien on cash, not a
withdrawal). Options are cash-settled at expiration for their intrinsic value —
there is no share assignment. Settling or closing releases the reserve, debits
whatever the position cost, and moves it to `history`.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
import threading

SCHEMA_VERSION = 1
DEFAULT_STARTING_BALANCE = 100_000.0
SHARES_PER_CONTRACT = 100

PUTS, CALLS = "puts", "calls"

# Statuses for positions still in book["open"]
OPEN, PENDING = "open", "pending_settlement"
# Terminal statuses, only found in book["history"]
EXPIRED, EXERCISED, CLOSED_EARLY, ABANDONED = (
    "expired_worthless", "exercised", "closed_early", "abandoned")

OUTCOME_LABELS = {
    EXPIRED: "Expired worthless",
    EXERCISED: "Exercised",
    CLOSED_EARLY: "Closed early",
    ABANDONED: "Abandoned",
}


class BookError(Exception):
    """The saved book is unusable."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _money(value: Any) -> float:
    """Round to cents so the JSON doesn't accumulate 0.30000000000000004."""
    return round(float(value), 2)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _pretty(iso: str) -> str:
    """Local copy of market.pretty_date — keeps this module dependency-free."""
    try:
        dt = datetime.strptime(iso, "%Y-%m-%d")
    except (ValueError, TypeError):
        return str(iso)
    return f"{dt:%b} {dt.day}, {dt:%Y}"


def describe(pos: dict) -> str:
    """'AAPL Aug 21, 2026 320 put' — used in messages and headings."""
    label = "put" if pos.get("kind") == PUTS else "call"
    return (f"{pos.get('symbol', '?')} {_pretty(pos.get('expiration', ''))} "
            f"{float(pos.get('strike', 0)):g} {label}")


# --------------------------------------------------------------------------- #
# Book construction
# --------------------------------------------------------------------------- #


def new_book(starting_balance: float = DEFAULT_STARTING_BALANCE) -> dict:
    starting = _money(starting_balance)
    return {
        "schema_version": SCHEMA_VERSION,
        "starting_balance": starting,
        "cash": starting,
        "next_position_id": 1,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "open": [],
        "history": [],
    }


# --------------------------------------------------------------------------- #
# Derived reads
# --------------------------------------------------------------------------- #


def reserve_for(kind: str, strike: float, spot: float, contracts: int = 1) -> float:
    """Cash locked up by selling this contract.

    A cash-secured put reserves the full assignment cost. A call reserves what
    the 100 covering shares would cost at today's price — a paper risk budget
    standing in for owning them, since nothing here holds actual shares.
    """
    per_share = float(strike) if kind == PUTS else float(spot)
    return _money(per_share * SHARES_PER_CONTRACT * int(contracts))


def intrinsic(kind: str, strike: float, price: float, contracts: int = 1) -> float:
    """Total dollars owed to the buyer if the contract settled at `price`."""
    if kind == PUTS:
        per_share = max(0.0, float(strike) - float(price))
    else:
        per_share = max(0.0, float(price) - float(strike))
    return _money(per_share * SHARES_PER_CONTRACT * int(contracts))


def reserved_collateral(book: dict) -> float:
    return _money(sum(p.get("reserved", 0.0) for p in book["open"]))


def available_cash(book: dict) -> float:
    """Cash not already pledged against an open position."""
    return _money(book["cash"] - reserved_collateral(book))


def realized_pnl(book: dict) -> float:
    return _money(sum(h.get("realized_pnl", 0.0) for h in book["history"]))


def open_premium(book: dict) -> float:
    return _money(sum(p.get("premium_collected", 0.0) for p in book["open"]))


def find_open(book: dict, position_id: int) -> dict | None:
    return next((p for p in book["open"] if p["id"] == position_id), None)


def unrealized_liability(book: dict, spots: dict[str, float | None]) -> tuple[float, list[str]]:
    """What every open position would cost if it settled at today's price.

    Returns (total, symbols whose price is unknown). Positions with no price are
    excluded from the total rather than counted as zero, so the UI can say so.
    """
    total, missing = 0.0, []
    for pos in book["open"]:
        spot = spots.get(pos["symbol"])
        if spot is None:
            missing.append(pos["symbol"])
            continue
        total += intrinsic(pos["kind"], pos["strike"], spot, pos.get("contracts", 1))
    return _money(total), sorted(set(missing))


def summary(book: dict, spots: dict[str, float | None] | None = None) -> dict:
    """Everything the playground header needs, in one pass."""
    spots = spots or {}
    unrealized, missing = unrealized_liability(book, spots)
    cash = _money(book["cash"])
    reserved = reserved_collateral(book)
    return {
        "starting_balance": _money(book["starting_balance"]),
        "cash": cash,
        "reserved": reserved,
        "available": _money(cash - reserved),
        "unrealized": unrealized,
        "missing_prices": missing,
        "realized": realized_pnl(book),
        "equity": _money(cash - unrealized),
        "open_count": len(book["open"]),
        "history_count": len(book["history"]),
    }


def check_invariant(book: dict, tol: float = 0.01) -> str | None:
    """Cash must equal the starting balance plus everything that moved it.

    Returns a description of the drift, or None when the book is consistent.
    Callers surface this; they must never silently "repair" it, because
    rewriting a balance to match a bug is worse than showing the bug.
    """
    expected = book["starting_balance"] + open_premium(book) + realized_pnl(book)
    drift = book["cash"] - expected
    if abs(drift) <= tol:
        return None
    return (f"Balance drift of ${drift:,.2f}: cash is ${book['cash']:,.2f} but "
            f"starting ${book['starting_balance']:,.2f} + open premiums "
            f"${open_premium(book):,.2f} + realized ${realized_pnl(book):,.2f} "
            f"= ${expected:,.2f}.")


# --------------------------------------------------------------------------- #
# Mutations — each returns (ok, message) and never raises on bad input
# --------------------------------------------------------------------------- #


def sell_position(book: dict, *, symbol: str, kind: str, expiration: str,
                  strike: float, premium_per_share: float, spot_at_sale: float,
                  contracts: int = 1, quote_fetched_at: str | None = None,
                  sold_at: str | None = None) -> tuple[bool, str]:
    """Sell to open. Credits the premium and locks the reserve.

    Duplicates are allowed on purpose: selling the same contract twice is two
    real contracts that settle independently. (The explorer rejects duplicate
    *rows*, which is a different thing — a row is a view, a position is a trade.)
    """
    if kind not in (PUTS, CALLS):
        return False, f"Unknown contract type {kind!r}."
    if premium_per_share is None or premium_per_share <= 0:
        return False, "No live bid, so there's no premium to collect."
    if strike is None or strike <= 0 or spot_at_sale is None or spot_at_sale <= 0:
        return False, "That contract is missing a strike or a spot price."
    contracts = max(1, int(contracts))

    reserve = reserve_for(kind, strike, spot_at_sale, contracts)
    available = available_cash(book)
    if available < reserve:
        return False, (f"Needs ${reserve:,.2f} available to sell; you have "
                       f"${available:,.2f}.")

    premium = _money(float(premium_per_share) * SHARES_PER_CONTRACT * contracts)
    position = {
        "id": book["next_position_id"],
        "symbol": symbol,
        "kind": kind,
        "expiration": expiration,
        "strike": float(strike),
        "contracts": contracts,
        "premium_per_share": _money(premium_per_share),
        "premium_collected": premium,
        "reserved": reserve,
        "spot_at_sale": _money(spot_at_sale),
        "sold_at": sold_at or _now_iso(),
        "quote_fetched_at": quote_fetched_at,
        "status": OPEN,
        "settle_error": None,
    }
    book["next_position_id"] += 1
    book["cash"] = _money(book["cash"] + premium)
    book["open"].append(position)
    return True, f"Sold {describe(position)} for ${premium:,.2f}."


def _retire(book: dict, pos: dict, *, status: str, cost_to_close: float,
            closed_at: str | None = None, exercised: bool | None = None,
            settlement_price: float | None = None,
            settlement_price_date: str | None = None,
            note: str = "") -> dict:
    """Move a position out of `open` into `history`, releasing its reserve.

    The reserve is released simply by removing the position — `reserved_collateral`
    sums over `book["open"]`, so there is no separate counter to keep in sync.
    """
    cost = _money(cost_to_close)
    book["cash"] = _money(book["cash"] - cost)
    book["open"] = [p for p in book["open"] if p["id"] != pos["id"]]

    record = dict(pos)
    record.update({
        "status": status,
        "closed_at": closed_at or _now_iso(),
        "exercised": exercised,
        "settlement_price": None if settlement_price is None else _money(settlement_price),
        "settlement_price_date": settlement_price_date,
        "settlement_note": note,
        "cost_to_close": cost,
        "realized_pnl": _money(pos["premium_collected"] - cost),
    })
    record.pop("settle_error", None)
    book["history"].append(record)
    return record


def settle_position(book: dict, position_id: int, final_close: float,
                    price_date: str, note: str = "",
                    settled_at: str | None = None) -> tuple[bool, str]:
    """Settle an expired position against its closing price.

    The buyer exercises whenever the contract has intrinsic value. Exactly
    at-the-money counts as not exercised — real markets have pin risk and
    contrary-exercise instructions, which this deliberately doesn't model.
    """
    pos = find_open(book, position_id)
    if pos is None:
        return False, "That position is no longer open."
    if final_close is None or final_close <= 0:
        return False, f"Refusing to settle {describe(pos)} against {final_close}."

    owed = intrinsic(pos["kind"], pos["strike"], final_close, pos.get("contracts", 1))
    exercised = owed > 0
    record = _retire(
        book, pos,
        status=EXERCISED if exercised else EXPIRED,
        cost_to_close=owed,
        closed_at=settled_at,
        exercised=exercised,
        settlement_price=final_close,
        settlement_price_date=price_date,
        note=note,
    )
    if exercised:
        return True, (f"{describe(pos)} finished at ${final_close:,.2f} and was "
                      f"exercised — paid ${owed:,.2f}, net "
                      f"${record['realized_pnl']:,.2f}.")
    return True, (f"{describe(pos)} finished at ${final_close:,.2f} and expired "
                  f"worthless — kept ${record['realized_pnl']:,.2f}.")


def buy_to_close(book: dict, position_id: int, ask_per_share: float,
                 closed_at: str | None = None) -> tuple[bool, str]:
    """Exit before expiry by buying the contract back at the ask."""
    pos = find_open(book, position_id)
    if pos is None:
        return False, "That position is no longer open."
    if ask_per_share is None or ask_per_share < 0:
        return False, "No ask is quoted, so this contract can't be priced to close."

    cost = _money(float(ask_per_share) * SHARES_PER_CONTRACT * pos.get("contracts", 1))
    record = _retire(book, pos, status=CLOSED_EARLY, cost_to_close=cost,
                     closed_at=closed_at, exercised=None)
    return True, (f"Closed {describe(pos)} for ${cost:,.2f} — net "
                  f"${record['realized_pnl']:,.2f}.")


def abandon_position(book: dict, position_id: int, reason: str = "") -> tuple[bool, str]:
    """Give up on a position that can never settle (delisted ticker, say).

    Without this its reserve stays locked forever. Costs nothing and keeps the
    premium, which is the honest outcome — the contract simply stopped existing.
    """
    pos = find_open(book, position_id)
    if pos is None:
        return False, "That position is no longer open."
    _retire(book, pos, status=ABANDONED, cost_to_close=0.0, exercised=None,
            note=reason or "Abandoned; no settlement price was available.")
    return True, f"Abandoned {describe(pos)} and released its reserve."


def mark_pending(book: dict, position_id: int, error: str) -> tuple[bool, str]:
    """Flag a position that's expired but couldn't be priced yet."""
    pos = find_open(book, position_id)
    if pos is None:
        return False, "That position is no longer open."
    pos["status"] = PENDING
    pos["settle_error"] = error
    return True, f"{describe(pos)} is waiting on a settlement price."


def reset_book(book: dict, starting_balance: float) -> tuple[bool, str]:
    """Wipe everything and start over. Mutates in place so the Store keeps its
    reference to the same dict."""
    fresh = new_book(starting_balance)
    fresh["created_at"] = _now_iso()
    book.clear()
    book.update(fresh)
    return True, f"Playground reset to ${book['starting_balance']:,.2f}."


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def save_book(book: dict, path: Path) -> None:
    """Write atomically: a crash mid-write must never truncate the book.

    Raises OSError, which the caller turns into a visible warning rather than a
    lost session.
    """
    book["updated_at"] = _now_iso()
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(book, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)  # atomic within one filesystem


def _coerce(raw: dict) -> dict:
    """Fill in anything a hand-edited file left out, without inventing money."""
    book = dict(raw)
    book.setdefault("schema_version", SCHEMA_VERSION)
    book["starting_balance"] = _money(book.get("starting_balance", DEFAULT_STARTING_BALANCE))
    book["cash"] = _money(book.get("cash", book["starting_balance"]))
    book["open"] = [p for p in book.get("open") or [] if isinstance(p, dict)]
    book["history"] = [h for h in book.get("history") or [] if isinstance(h, dict)]
    book.setdefault("created_at", _now_iso())
    book.setdefault("updated_at", _now_iso())

    for pos in book["open"]:
        pos.setdefault("contracts", 1)
        pos.setdefault("status", OPEN)
        pos.setdefault("settle_error", None)
        pos.setdefault("quote_fetched_at", None)
        pos["strike"] = float(pos.get("strike", 0.0))
        pos["premium_collected"] = _money(pos.get("premium_collected", 0.0))
        pos["reserved"] = _money(pos.get("reserved", 0.0))
    for rec in book["history"]:
        rec.setdefault("contracts", 1)
        rec["premium_collected"] = _money(rec.get("premium_collected", 0.0))
        rec["cost_to_close"] = _money(rec.get("cost_to_close", 0.0))
        rec["realized_pnl"] = _money(
            rec.get("realized_pnl", rec["premium_collected"] - rec["cost_to_close"]))

    ids = [p["id"] for p in book["open"] if isinstance(p.get("id"), int)]
    ids += [h["id"] for h in book["history"] if isinstance(h.get("id"), int)]
    book["next_position_id"] = max(book.get("next_position_id", 1) or 1,
                                   (max(ids) + 1) if ids else 1)
    return book


def _migrate(raw: dict) -> tuple[dict, str | None]:
    """Bring an older book up to SCHEMA_VERSION.

    Only v1 exists today, so this is a placeholder with the shape that future
    steps slot into (`if version < 2: ...`).
    """
    version = raw.get("schema_version", 0)
    if version == SCHEMA_VERSION:
        return _coerce(raw), None
    book = _coerce(raw)
    book["schema_version"] = SCHEMA_VERSION
    return book, f"Upgraded the saved playground from schema v{version} to v{SCHEMA_VERSION}."


def load_book(path: Path) -> tuple[dict, str | None, bool]:
    """Read the book from disk.

    Returns (book, warning, read_only). A missing file is not an error and is
    not written back — an empty directory shouldn't sprout files just from
    opening the app. A corrupt file is preserved under a new name rather than
    overwritten, because it's the only copy of the user's trades.
    """
    if not path.exists():
        return new_book(), None, False

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.stem}.corrupt-{stamp}.json")
        try:
            os.replace(path, backup)
            kept = f" The unreadable file was kept as {backup.name}."
        except OSError:
            kept = ""
        return new_book(), (f"Could not read {path.name} ({exc}); started a fresh "
                            f"playground.{kept}"), False

    if not isinstance(raw, dict):
        return new_book(), f"{path.name} isn't a playground file; started a fresh one.", False

    version = raw.get("schema_version", 0)
    if isinstance(version, int) and version > SCHEMA_VERSION:
        # Newer app wrote this. Show it, but refuse to write over it.
        return _coerce(raw), (
            f"{path.name} was written by a newer version of this app (schema v{version}). "
            "It's shown read-only so nothing is lost — update the app to trade again."
        ), True

    book, warning = _migrate(raw)
    return book, warning, False


# --------------------------------------------------------------------------- #
# Process-global store
# --------------------------------------------------------------------------- #


def book_path() -> Path:
    override = os.environ.get("PAPER_BOOK_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent / "paper_book.json"


class Store:
    """One book per server process, shared by every browser tab.

    The book can't live in st.session_state: that's per-tab, so two tabs would
    each hold a divergent copy and the last one to save would silently discard
    the other's trades. A single process-global Store means both tabs mutate the
    same dict and the file always reflects it.

    Streamlit runs each session in its own thread, so every mutation takes a
    lock across read-modify-write-persist.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or book_path()
        self.book, self.load_warning, self.read_only = load_book(self.path)
        self.save_error: str | None = None
        self.last_sweep_at: float = 0.0
        self._lock = threading.Lock()

        drift = check_invariant(self.book)
        if drift:
            self.load_warning = " ".join(filter(None, [self.load_warning, drift]))

    def mutate(self, fn: Callable[..., tuple[bool, str]], *args, **kwargs) -> tuple[bool, str]:
        """Apply a mutation and persist it, atomically with respect to other tabs."""
        if self.read_only:
            return False, "The saved playground is read-only (written by a newer version)."
        with self._lock:
            try:
                ok, message = fn(self.book, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - a bug here must not blank the page
                return False, f"Could not complete that: {exc}"
            if not ok:
                return ok, message
            try:
                save_book(self.book, self.path)
                self.save_error = None
            except OSError as exc:
                # Keep the in-memory change so the session still works, but say so.
                self.save_error = (f"Could not save to {self.path.name}: {exc}. "
                                   "Changes will be lost when the app restarts.")
            return ok, message

    def invariant_warning(self) -> str | None:
        return check_invariant(self.book)

    def due_for_settlement(self, is_past_close: Callable[[str], bool]) -> list[dict]:
        """Open positions whose expiration close has passed. Pure arithmetic —
        no network — so it's cheap enough to call on every render."""
        return [p for p in self.book["open"] if is_past_close(p["expiration"])]
