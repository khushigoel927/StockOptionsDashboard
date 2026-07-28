# 📈 Options Selling Explorer

A small Streamlit dashboard for eyeballing **cash-secured puts** and **covered calls** with live
option-chain data from Yahoo Finance, plus a paper-trading playground for practising selling them.

Two tabs:

- **Explorer** — scratch cards answering *what would this contract look like right now?* Not a
  position tracker; rows live in `st.session_state` and disappear when the browser session ends.
- **Playground** — sell those contracts on paper. Tracks a balance, settles positions against the
  real closing price at expiration, and keeps a permanent history. Saved to disk.

Nothing in this app is connected to a broker, and nothing here places a trade.

---

## What it shows

Add a row by entering a ticker, an expiration and a strike. The app snaps to the nearest listed
strike and renders a card with:

| Field | Meaning |
| --- | --- |
| **Bid / ask** | Current quote for the contract |
| **Open interest / volume** | Liquidity inputs behind the badge |
| **Premium collected** | `bid × 100` — what you'd take in for one contract |
| **Cash to secure** *(puts)* | `strike × 100` — cash set aside for a cash-secured put |
| **Requires** *(calls)* | 100 shares owned per contract for a covered call |
| **Move needed to exercise** | Distance from spot to strike, in dollars and percent |

Each card carries a liquidity badge:

- **Liquid** — bid is live and both open interest and volume clear your thresholds.
- **Thin** — tradeable, but open interest and/or volume fall below the sidebar thresholds.
- **No bid** — no live bid, so the contract isn't tradeable right now.

Thresholds are configurable in the sidebar (defaults: open interest ≥ 100, daily volume ≥ 10).

Editing a card's strike or expiration re-prices it immediately. **Refresh all** clears the quote
cache and re-pulls every row.

As you type a symbol, the line under the add form reports the strike range that has a **live bid**,
not every listed strike:

> AAPL spot $335.14 · **10 of 39 strikes have a live bid** for Jul 27, 2026 ($330.00 – $362.50) ·
> nearest to spot 335

Deep out-of-the-money strikes stay listed long after anyone stops quoting them, so the full listed
range advertises contracts you couldn't actually sell. The "nearest to spot" suggestion — which
seeds the strike box — is drawn from the quotable strikes too.

## The playground

Every explorer card has a **Sell put** / **Sell call** button. It's paper only — it records the
position on the Playground tab and places no order. The button greys out with the reason when a sale
can't happen (no live bid, already expired, not enough available cash).

Once sold, the card keeps a marker so you can see it landed:

> ✓ **On the Playground** — 1 open position, $530.00 premium collected.

and the button becomes **Sell another put**. After the contract settles or you close it, the marker
turns grey and reports history instead. The marker follows the *contract*, not the card: edit a
card's strike and it disappears, because the card now describes something else. Deleting a card
never touches the position.

A fresh playground starts with **$200,000**. You can set a different figure whenever you reset.

### How the accounting works

| Term | Meaning |
| --- | --- |
| **Cash balance** | Premiums land here the moment you sell. Settlement costs come out of it. |
| **Reserved** | Locked while a position is open — `strike × 100` for a put, `spot × 100` for a call (the cash to buy the 100 covering shares). Released in full when the position closes. |
| **Available cash** | Cash minus reserved. New sales are checked against this. |
| **Unrealized liability** | What you'd owe if every open position settled at today's price. |
| **Realized P&L** | Premiums kept, minus what settled and closed positions cost. |

The balance always satisfies
`cash = starting balance + premiums on open positions + realized P&L`. The app checks this on load
and after every change, and warns rather than silently "fixing" a mismatch.

### What each open position shows

| Premium collected | Spot price | P&L if settled now | P&L if closed now |
| --- | --- | --- | --- |
| $525.00 | $335.65 ↑ 5.00 | **$115.00** | **−$300.00** |

The two P&L figures answer different questions, and the gap between them is the contract's remaining
time value:

- **P&L if settled now** — premium minus the intrinsic value you'd owe if it expired at today's spot.
- **P&L if closed now** — premium minus the current **ask**. The one you can act on, and exactly what
  **Buy to close** will charge you.

A position often opens slightly *negative* on the second figure: you sell at the bid and buy back at
the ask, so you're down the spread from the start and need time decay to turn it around.

The spot delta's colour flips by contract type — for a short put a rising stock is good news, for a
short call it isn't.

Underneath, a caption carries the reserve, the cost to buy back, what's owed if settled now, and the
original sale record.

### Live refresh

While the Playground tab is open it re-prices itself on a timer, no clicking required:

- **Spot — every 20 seconds** (the cache expires at 15s, so every tick fetches genuinely new data
  rather than re-rendering a still-valid entry).
- **Close prices — every 60 seconds.** Option chains are a far larger payload and asks move more
  slowly than the last trade.

Because the tabs are lazy, none of this polls while you're on the Explorer. Quotes still carry
Yahoo's own delay regardless of how often they're fetched.

### Settlement

Positions settle **in cash at intrinsic value** using the underlying's actual close on the
expiration date:

- Short put — you owe `max(0, strike − final close) × 100`
- Short call — you owe `max(0, final close − strike) × 100`

The buyer exercises whenever that number is above zero. No shares change hands, so a call's reserve
is a paper risk budget rather than real share ownership — there is no share P&L to offset a call
going against you.

Settlement runs when you open the Playground tab, never before the 4:00 pm ET close on the
expiration date. If a price can't be fetched, the position is flagged **awaiting price**, keeps its
reserve, and retries — it is never settled at a guess. A position whose ticker has died can be
**abandoned** to release its reserve.

### Closing early and resetting

**Buy to close** pays the current ask × 100 to exit before expiry and books the P&L to history.
**Reset playground** wipes all positions and history and starts again from a balance you choose.

### Where it's saved

`paper_book.json`, next to `app.py` (override with the `PAPER_BOOK_PATH` environment variable). It's
gitignored — it's your state, not code. Writes are atomic, so a crash can't truncate it, and an
unreadable file is preserved as `paper_book.corrupt-<timestamp>.json` rather than overwritten.

One book is shared by every browser tab against a running server. Running **two server processes**
on the same directory is the one thing that will lose trades — last writer wins. `app.sh` guards
against that with its pidfile.

## Requirements

- Python 3.10+ (the code uses `X | None` type syntax)
- Streamlit 1.60+ (the app relies on lazy tabs and `persist_state`)
- Internet access — quotes come from Yahoo Finance via [`yfinance`](https://github.com/ranaroussi/yfinance)

## Setup

```bash
git clone <your-repo-url>
cd StockOptionsDashboard

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running

Use the control script — it runs Streamlit headless in the background on port 8555:

```bash
./app.sh start     # start in background → http://localhost:8555
./app.sh stop      # stop it
./app.sh restart   # stop + start
./app.sh status    # is it running?
./app.sh logs      # tail the log live
```

Override the port with `PORT=9000 ./app.sh start`.

Or run it in the foreground directly:

```bash
streamlit run app.py
```

## Project layout

```
app.py            explorer UI, tab wiring, and the Sell buttons
market.py         every yfinance call — quotes, chains, settlement prices
paper.py          the paper-trading book: schema, persistence, accounting
playground.py     playground UI, the settlement sweep, and the shared Store
app.sh            start/stop/restart/status/logs wrapper around Streamlit
requirements.txt  streamlit, yfinance, pandas, tzdata
howToRun.txt      the app.sh cheat sheet
```

- **`market.py`** — `fetch_expirations`, `fetch_spot`, `fetch_chain`, `fetch_strikes`,
  `fetch_quotable_strikes` and `fetch_ask`, all wrapped in `st.cache_data` with TTLs tuned to how
  fast each thing actually moves (15s for spot, 60s for chains, 15m for expiration lists);
  `fetch_settlement_close` is cached forever because a past date's close never changes. Every
  provider failure is normalized into a `DataError` so a broken row shows a message, not a
  traceback. `price_contract` finds the nearest listed strike and computes premium, cash required,
  and the move needed for the buyer to exercise.
- **`paper.py`** — imports neither Streamlit nor yfinance, so the accounting can be exercised from a
  plain REPL. Holds the JSON schema, atomic saves, and every mutation (`sell_position`,
  `settle_position`, `buy_to_close`, `abandon_position`, `reset_book`), each returning
  `(ok, message)`.
- **`playground.py`** — owns the `Store` (one book per server process, not per browser tab, so two
  tabs can't clobber each other), the settlement sweep, the auto-refreshing `st.fragment`, and the
  playground's rendering.
- **`app.py`** — the explorer cards and the `st.tabs` wiring. The tabs are lazy (`on_change="rerun"`
  plus `.open`), so the playground makes no network calls while you're typing in the explorer.

One non-obvious detail: money rendered through Streamlit markdown is escaped as `\$` via the `md()`
and `usd()` helpers in `playground.py`. Streamlit reads a pair of unescaped `$` in one string as
inline LaTeX, which silently turns a caption full of dollar amounts into an equation. The explorer's
cards are exempt because their numbers sit inside `<div>` blocks, which CommonMark passes through
without parsing inline markdown.

## Caveats

- Yahoo Finance data is delayed and occasionally missing or wrong. Treat everything here as a
  rough sketch, not a quote you can trade against.
- Premium uses the **bid** and closing uses the **ask** — you cross the spread both ways, which is
  the pessimistic and realistic direction.
- No commissions or fees.
- Settlement is cash-at-intrinsic. Real US equity options are physically settled: you'd be assigned
  100 actual shares. There's no early exercise, no pin risk (finishing exactly at the strike counts
  as not exercised), and no share P&L on covered calls.
- A short call's loss isn't capped by its reserve, so a large gap up can push the balance negative.
  The app shows this rather than clamping it.
- Index options like `^SPX` can be entered, but they settle against a special opening quotation in
  reality, not the close this app uses.
- Explorer rows are still not persisted — they're gone when the browser session ends. Only the
  playground is saved.

## Disclaimer

For educational and exploratory use only. This is not financial advice, and nothing in this app
places a trade.
