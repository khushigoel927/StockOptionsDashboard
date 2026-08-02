"""
The paper-trading book: schema, persistence, and all of the accounting.

Deliberately imports nothing from streamlit or yfinance. A sign error in this
module would quietly corrupt someone's balance, so it has to be drivable from a
plain REPL:

    from core import paper
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
from uuid import uuid4
import threading

SCHEMA_VERSION = 1
DEFAULT_STARTING_BALANCE = 200_000.0
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


def new_player() -> dict:
    """A book's identity, for anything that compares books across people.

    Random and self-assigned — it identifies a *book*, not a person, and there's
    nothing to look up. It rides inside the book on purpose: export a book to
    another device and your identity travels with it, which is what you'd want.
    The flip side is that two people who import the same file share an id, and
    any leaderboard has to be built expecting that.

    `name` stays None until someone chooses to be listed somewhere.
    """
    return {"id": uuid4().hex[:12], "name": None}


def new_book(starting_balance: float = DEFAULT_STARTING_BALANCE) -> dict:
    starting = _money(starting_balance)
    return {
        "schema_version": SCHEMA_VERSION,
        "starting_balance": starting,
        "cash": starting,
        "next_position_id": 1,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "player": new_player(),
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


def replace_book(book: dict, incoming: dict) -> tuple[bool, str]:
    """Swap the live book's contents for an imported one.

    Mutates in place so the Store keeps its reference to the same dict, exactly
    like `reset_book`. Goes through `Store.mutate` so the import takes the lock
    and is persisted like any other change.
    """
    if not isinstance(incoming, dict) or "cash" not in incoming:
        return False, "That doesn't look like a playground book."
    book.clear()
    book.update(incoming)
    return True, (f"Loaded a book with {len(book['open'])} open position(s), "
                  f"{len(book['history'])} closed, and ${book['cash']:,.2f} cash.")


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


def dump_book(book: dict, *, compact: bool = False) -> str:
    """The book as JSON text — for a download, or for browser storage.

    `compact` drops the indentation, which is worth roughly a third of the bytes
    when the destination is a storage quota rather than a file someone reads.
    """
    book["updated_at"] = _now_iso()
    if compact:
        return json.dumps(book, separators=(",", ":"))
    return json.dumps(book, indent=2)


def parse_book(text: str | bytes) -> tuple[dict, str | None]:
    """Read a book from JSON text. Returns (book, warning) and never raises.

    The counterpart to `dump_book`, and the only way a book should enter the app
    from outside: it runs the same `_migrate`/`_coerce` path as a file on disk,
    so an old export upgrades and a truncated one is rejected rather than
    half-loaded.

    Deliberately does NOT check the balance invariant — that's the caller's job
    via `check_invariant`, because a book that doesn't add up should still be
    *shown*, with a warning, rather than thrown away. It may be the only copy of
    someone's trades.
    """
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            return new_book(), "That file isn't UTF-8 text, so it isn't a playground book."
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        return new_book(), f"That isn't valid JSON ({exc}), so nothing was loaded."
    if not isinstance(raw, dict):
        return new_book(), "That JSON isn't a playground book (expected an object)."
    if "cash" not in raw and "open" not in raw and "history" not in raw:
        # Valid JSON, but nothing book-shaped: better to say so than to hand
        # back a pristine $200,000 book and let it look like a successful load.
        return new_book(), "That JSON has no book in it (no cash, open or history)."

    version = raw.get("schema_version", 0)
    if isinstance(version, int) and version > SCHEMA_VERSION:
        return _coerce(raw), (f"This book was written by a newer version of the app "
                              f"(schema v{version}); some of it may not be understood.")
    return _migrate(raw)


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
    # Backfilled rather than migrated: books are already saved in people's
    # browsers, and giving an old one an id on load is cheaper than a schema
    # bump — it changes nothing about the money.
    player = book.get("player")
    if not isinstance(player, dict) or not player.get("id"):
        book["player"] = new_player()
    else:
        player.setdefault("name", None)

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
# Backends — where a Store reads and writes its book
# --------------------------------------------------------------------------- #


class Backend:
    """A place a book can be kept.

    Exists so that `Store` doesn't know whether it's backed by a file, a
    browser, a database, or nothing at all. Everything above it — the settlement
    sweep, the renderers, the mutations — talks to a `Store` and never to
    storage, so a new backend is additive rather than a change to the accounting.

    `label` names the destination in error messages ("Could not save to ...").
    """

    label = "nowhere"

    # True when the backend keeps a copy somewhere the user would reasonably
    # want to be able to delete — their browser, say. Drives whether the UI
    # offers to erase it, so that "where is my data" has an answer that isn't
    # "nowhere you can reach".
    erasable = False

    # A persistence problem that isn't tied to any one mutation — a browser
    # refusing to write, say. Kept apart from `Store.save_error` because that
    # one is set and cleared per mutation, and a standing problem must not be
    # cleared by the next trade happening to "succeed".
    status_error: str | None = None

    def load(self) -> tuple[dict, str | None, bool]:
        """Return (book, warning to show the user, read_only)."""
        raise NotImplementedError

    def save(self, book: dict) -> None:
        """Persist the book, or raise OSError. Called after every mutation."""
        raise NotImplementedError

    def erase(self) -> None:
        """Delete the persisted copy. Only called when `erasable`."""
        raise NotImplementedError


class NullBackend(Backend):
    """Keeps the book in memory and nothing else — the public deployment.

    Loading always yields a fresh book, saving does nothing. Not a degraded
    FileBackend: on a shared server there is no file that could belong to one
    visitor, so "nowhere" is the correct destination rather than a missing one.
    """

    label = "memory"

    def __init__(self, starting_balance: float = DEFAULT_STARTING_BALANCE) -> None:
        self.starting_balance = starting_balance

    def load(self) -> tuple[dict, str | None, bool]:
        return new_book(self.starting_balance), None, False

    def save(self, book: dict) -> None:
        pass


class FileBackend(Backend):
    """A JSON file on local disk — the bots app.

    Only correct where the process owns the file: one book, every tab and bot
    sharing it. Useless on a host with an ephemeral or shared filesystem.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.label = path.name

    def load(self) -> tuple[dict, str | None, bool]:
        return load_book(self.path)

    def save(self, book: dict) -> None:
        save_book(book, self.path)


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


def book_path() -> Path:
    override = os.environ.get("PAPER_BOOK_PATH")
    if override:
        return Path(override).expanduser().resolve()
    # Project root, not this package — the book is user data, not source.
    return Path(__file__).resolve().parent.parent / "paper_book.json"


class Store:
    """A book, the lock that guards it, and the backend it's persisted to.

    The backend decides the sharing model, and the two are not interchangeable:

    * `NullBackend` — one Store per visitor, held in session state. Correct on a
      shared server, where a process-global book would hand everyone the same
      balance.
    * `FileBackend` — one Store per process, shared by every tab and any bot.
      Such a book must NOT live in st.session_state: that's per-session, so two
      tabs would each hold a divergent copy and the last save would silently
      discard the other's trades.

    Streamlit runs each session in its own thread, so every mutation takes a
    lock across read-modify-write-persist.
    """

    def __init__(self, backend: Backend | None = None) -> None:
        self.backend = backend or NullBackend()
        self.book, self.load_warning, self.read_only = self.backend.load()
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
                self.backend.save(self.book)
                self.save_error = None
            except OSError as exc:
                # Keep the in-memory change so the session still works, but say so.
                self.save_error = (f"Could not save to {self.backend.label}: {exc}. "
                                   "Changes will be lost when the app restarts.")
            return ok, message

    @property
    def persistence_error(self) -> str | None:
        """Anything stopping this book from being saved, whatever the cause."""
        return self.save_error or self.backend.status_error

    def invariant_warning(self) -> str | None:
        return check_invariant(self.book)

    def due_for_settlement(self, is_past_close: Callable[[str], bool]) -> list[dict]:
        """Open positions whose expiration close has passed. Pure arithmetic —
        no network — so it's cheap enough to call on every render."""
        return [p for p in self.book["open"] if is_past_close(p["expiration"])]
