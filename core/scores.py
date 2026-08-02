"""Turning a book into a comparable score, and checking whether to believe it.

Groundwork for a leaderboard. Everything here is pure of any particular
storage: `scorecard` produces the row, `audit` says how much of it survives
scrutiny, and `Leaderboard` is the socket a real backend plugs into. No
leaderboard is wired up yet — see `NullLeaderboard`.

What can and cannot be verified
-------------------------------
Read this before designing anything competitive on top.

**Verifiable.** Settlement is deterministic: an option expiring on a given date
settles at the underlying's actual close, which `market.fetch_settlement_close`
can re-fetch for any past date. So a server can recompute every settled trade's
P&L from scratch and reject a book whose history doesn't match. The balance
identity (`cash = starting + open premiums + realized`) is checkable too.

**Not verifiable.** The premium someone claims to have collected. yfinance
serves live option quotes and historical *stock* closes — there is no
historical option quote to check a claimed fill against. A book asserting it
sold a far-OTM put for $50 a share is arithmetically consistent and
unfalsifiable from here.

That limit is not a property of where the book is stored, and accounts and a
database would not fix it. The only real fix is for trades to go through the
server as they happen, so the server records the price rather than being told
it. Until then a leaderboard here is a fun board, not a contest — and `audit`
is a filter for the careless, not a defence against the determined.
"""

from __future__ import annotations

from datetime import date

from . import market, paper

# Enough of a difference to be a real discrepancy rather than rounding.
SETTLE_TOLERANCE = 0.51


def scorecard(book: dict, spots: dict[str, float | None] | None = None) -> dict:
    """The row a leaderboard would show for this book.

    `spots` is optional but changes what the score means. Without it, only
    *realized* P&L is known — and a board ranked on realized alone rewards
    holding losers open forever while banking winners. With it, `equity` marks
    open positions to market and is the honest number to rank on; `complete`
    says which of the two you got.
    """
    totals = paper.summary(book, spots or {})
    closed = book["history"]
    wins = [h for h in closed if h.get("realized_pnl", 0.0) > 0]
    started = float(book.get("starting_balance") or 0.0)
    complete = bool(spots) and not totals["missing_prices"]

    return {
        "player_id": (book.get("player") or {}).get("id"),
        "name": (book.get("player") or {}).get("name"),
        "equity": totals["equity"] if complete else None,
        "realized": totals["realized"],
        "return_pct": round(totals["realized"] / started * 100, 2) if started else 0.0,
        "closed_trades": len(closed),
        "open_positions": totals["open_count"],
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "best_trade": max((h.get("realized_pnl", 0.0) for h in closed), default=None),
        "worst_trade": min((h.get("realized_pnl", 0.0) for h in closed), default=None),
        "starting_balance": started,
        "started_at": book.get("created_at"),
        "updated_at": book.get("updated_at"),
        # False when open positions couldn't all be priced, so a consumer can
        # refuse to rank the row rather than ranking a number that means
        # something different from every other row.
        "complete": complete,
    }


def audit(book: dict, *, recheck_settlements: bool = True) -> list[str]:
    """Everything suspect about this book, most damning first. Empty means clean.

    Best-effort by construction — see the module docstring for the premium
    problem. Never raises: a provider failure means a settlement goes unchecked,
    which is reported as such rather than counted as a pass.
    """
    findings: list[str] = []

    drift = paper.check_invariant(book)
    if drift:
        findings.append(f"Balance doesn't add up. {drift}")

    for pos in book["open"] + book["history"]:
        premium = pos.get("premium_collected", 0.0)
        if premium < 0:
            findings.append(f"{paper.describe(pos)} collected a negative premium "
                            f"(${premium:,.2f}).")
        contracts = pos.get("contracts", 1)
        per_share = pos.get("premium_per_share")
        if per_share is not None and abs(
                per_share * paper.SHARES_PER_CONTRACT * contracts - premium) > 0.02:
            findings.append(f"{paper.describe(pos)} claims ${per_share:,.2f}/share but "
                            f"banked ${premium:,.2f}.")
        # A short option can never be worth more than the strike (put) to its
        # seller, and a premium above the underlying's own price is nonsense.
        strike = float(pos.get("strike") or 0.0)
        if per_share is not None and strike and per_share > strike:
            findings.append(f"{paper.describe(pos)} sold for more per share "
                            f"(${per_share:,.2f}) than its strike (${strike:,.2f}).")

    for rec in book["history"]:
        if rec.get("status") != paper.EXPIRED and rec.get("status") != paper.EXERCISED:
            continue  # closed early or abandoned; there's no close to check against
        expiration = rec.get("expiration")
        if not _is_past(expiration):
            findings.append(f"{paper.describe(rec)} is recorded as settled but "
                            f"hasn't expired yet.")
            continue
        if not recheck_settlements:
            continue
        try:
            close, _, _ = market.fetch_settlement_close(rec["symbol"], expiration)
        except market.DataError as exc:
            findings.append(f"Couldn't verify {paper.describe(rec)}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - an audit must not crash on a bad ticker
            findings.append(f"Couldn't verify {paper.describe(rec)}: {exc}")
            continue

        owed = paper.intrinsic(rec["kind"], rec["strike"], close, rec.get("contracts", 1))
        claimed = rec.get("cost_to_close", 0.0)
        if abs(owed - claimed) > SETTLE_TOLERANCE:
            findings.append(
                f"{paper.describe(rec)} settled against ${close:,.2f}, which costs "
                f"${owed:,.2f} — the book recorded ${claimed:,.2f}.")

    return findings


def _is_past(expiration: str | None) -> bool:
    try:
        return date.fromisoformat(str(expiration)) <= date.today()
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# The socket a real leaderboard plugs into
# --------------------------------------------------------------------------- #


class Leaderboard:
    """Somewhere scores are collected across visitors.

    Necessarily server-side: a browser can only ever see its own book, so unlike
    the book itself this genuinely needs shared storage. On Streamlit Community
    Cloud that means an external database — the container's disk is wiped on
    every restart — reached through `st.connection` with credentials in the
    app's secrets, never in the repository.

    Submission must stay explicit and opt-in. A visitor's book is private by
    construction today, and quietly shipping it to a server the first time
    someone sells a put would undo the entire reason it lives in their browser.
    """

    configured = False

    def submit(self, card: dict) -> tuple[bool, str]:
        """Publish one scorecard. Returns (ok, message) like every mutation here."""
        raise NotImplementedError

    def top(self, limit: int = 20) -> list[dict]:
        """The best scorecards, best first."""
        raise NotImplementedError


class NullLeaderboard(Leaderboard):
    """What's installed today: nothing, said out loud.

    Lets callers ask `leaderboard.configured` and hide the UI, rather than
    offering a board that silently drops every submission.
    """

    configured = False

    def submit(self, card: dict) -> tuple[bool, str]:
        return False, "No leaderboard is configured for this deployment."

    def top(self, limit: int = 20) -> list[dict]:
        return []
