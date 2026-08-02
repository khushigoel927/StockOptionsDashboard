# 📈 Options Selling Explorer

A small Streamlit dashboard for eyeballing **cash-secured puts** and **covered calls** with live
option-chain data from Yahoo Finance, plus a paper-trading playground for practising selling them.

Two tabs:

- **Explorer** — scratch cards answering *what would this contract look like right now?* Not a
  position tracker; rows live in `st.session_state` and disappear when the browser session ends.
- **Playground** — sell those contracts on paper. Tracks a balance, settles positions against the
  real closing price at expiration, and keeps a history.

Nothing in this app is connected to a broker, and nothing here places a trade.

### Two apps, one codebase

The same UI ships in two configurations, differing only in where the book lives:

| | entrypoint | the book | who it's for |
|---|---|---|---|
| **Public** | `app.py` | **one per visitor**, saved in that visitor's own browser | deployed to Streamlit Community Cloud |
| **Local** | `bots_app.py` | one on disk (`paper_book.json`), shared by every tab | you, on localhost, plus an extra **Bots** tab |

The split exists because the playground keeps score. A single process-global book is right when one
person runs the server and wrong the moment strangers share it — everyone would spend the same
balance and settle each other's positions. So the public app scopes the book to `st.session_state`
and mirrors it into the visitor's own `localStorage` — your positions are yours, they survive
closing the tab, and they never reach the server.

`app.py` imports `core/` and nothing else, so the bot layer is absent from the deployment even
though it lives in the same repository. The arrow is one-way: `bots/` may import `core/`, `core/`
must never import `bots/`.

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

Depends on which app you're running — see [Two apps, one codebase](#two-apps-one-codebase).

**Public (`app.py`)** — in the visitor's browser, under the `localStorage` key
`options-playground/book/v1`. Nothing reaches the server, so there's no file that could leak one
visitor's trades to another, no database to run, and no data of anyone's to be responsible for. A
book survives closing the tab and every app restart; it does not follow you to another browser or
device, which is what **Save or load** is for.

That has a consequence worth stating plainly: the book is data the user controls, so anyone willing
to open dev tools can give themselves $10M. Fine for a playground, and the reason
`check_invariant` runs on every load — a book that doesn't add up is *shown with a warning* rather
than trusted or thrown away.

Storage can fail through no fault of the user (Safari private browsing throws on write, so does a
full quota). Those failures surface as a visible error with a nudge to download a copy, rather than
being swallowed into a book that silently stops saving.

**Local (`bots_app.py`)** — `paper_book.json` in the project root (override with `PAPER_BOOK_PATH`).
Gitignored: it's your state, not code. Writes are atomic, so a crash can't truncate it, and an
unreadable file is preserved as `paper_book.corrupt-<timestamp>.json` rather than overwritten.

That book is shared by every browser tab against the running server. Running **two server
processes** on it is the one thing that will lose trades — last writer wins. `app.sh` guards against
that with a per-app pidfile.

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

Use the control script — it runs Streamlit headless in the background. The second argument picks
the app, defaulting to `public`; each gets its own port, pidfile and log, so both can run at once:

```bash
./app.sh start          # public app  → http://localhost:8555
./app.sh start bots     # local + bots → http://localhost:8556
./app.sh stop bots      # stop it
./app.sh restart        # stop + start
./app.sh status bots    # is it running?
./app.sh logs           # tail the log live
```

Override the port with `PORT=9000 ./app.sh start`.

Or run either in the foreground directly:

```bash
streamlit run app.py        # what the world sees
streamlit run bots_app.py   # with the Bots tab and the saved book
```

## Deploying the public app

It's built for [Streamlit Community Cloud](https://share.streamlit.io) as-is — no secrets, no
database, no writable disk needed.

1. Push to GitHub.
2. On share.streamlit.io: **New app** → this repo → branch `main` → **main file `app.py`**.
3. Deploy.

The main-file setting is the whole of the split: point it at `app.py` and `bots/` is never
imported. Never point it at `bots_app.py` — that app shares one balance among everyone who opens
it.

Two things to know about that host: containers sleep when idle and restart on a redeploy, and each
restart clears every visitor's in-memory book. `st.cache_data` on the quote fetchers is shared
across visitors, which is what keeps a busy day from turning into one Yahoo request per person.

## Project layout

```
app.py            PUBLIC entrypoint — session-scoped book, no bots. This is what deploys.
bots_app.py       local entrypoint — disk-backed book, plus the Bots tab
core/             everything both apps share
  market.py       every yfinance call — quotes, chains, settlement prices
  paper.py        the paper-trading book: schema, persistence, accounting
  playground.py   playground UI and the settlement sweep
  explorer.py     explorer UI, the cards, and the Sell buttons
  shell.py        the page both apps draw: title, sidebar, toolbar, tabs
  stores.py       where the book lives — the one thing the two apps disagree on
  browser.py      the localStorage sync component (public app's persistence)
  scores.py       scorecards and book auditing — groundwork for a leaderboard
bots/             local-only bot layer (empty; see its docstring for the seam)
app.sh            start/stop/restart/status/logs wrapper around Streamlit
requirements.txt  streamlit, yfinance, pandas, tzdata
howToRun.txt      the app.sh cheat sheet
```

- **`core/paper.py` backends** — `Store` reads and writes through a `Backend` rather than a
  path, so where a book lives is a swap rather than a change to the accounting. `NullBackend`
  keeps it in memory (public), `FileBackend` on disk (local).
- **`core/browser.py`** — a JS-only CCv2 component that mirrors the book into `localStorage`. The
  handshake is two-phase: on the first render Python has nothing, so the component reports what's
  stored and triggers one more rerun; from then on Python is authoritative and the component writes
  when the book changes. The first render is gated behind a "restoring" message because without it
  a returning visitor would see a fresh $200,000 book flash before their real one loaded, which
  looks exactly like data loss.
- **`core/stores.py`** — the load-bearing file. `session_store()` is deliberately *not*
  `@st.cache_resource` (process-global would hand every visitor the same balance);
  `shared_store()` deliberately *is*, because bots and browser tabs in the local app have to mutate
  one book or their writes clobber each other. Each function's docstring says why the other one
  would be a bug there.
- **`core/market.py`** — `fetch_expirations`, `fetch_spot`, `fetch_chain`, `fetch_strikes`,
  `fetch_quotable_strikes` and `fetch_ask`, all wrapped in `st.cache_data` with TTLs tuned to how
  fast each thing actually moves (15s for spot, 60s for chains, 15m for expiration lists);
  `fetch_settlement_close` is cached forever because a past date's close never changes. Every
  provider failure is normalized into a `DataError` so a broken row shows a message, not a
  traceback. `price_contract` finds the nearest listed strike and computes premium, cash required,
  and the move needed for the buyer to exercise.
- **`core/paper.py`** — imports neither Streamlit nor yfinance, so the accounting can be exercised from a
  plain REPL. Holds the JSON schema, atomic saves, and every mutation (`sell_position`,
  `settle_position`, `buy_to_close`, `abandon_position`, `reset_book`), each returning
  `(ok, message)`.
- **`core/playground.py`** — the settlement sweep, the auto-refreshing `st.fragment`, and the
  playground's rendering.
- **`core/explorer.py`** — the explorer cards. **`core/shell.py`** — the `st.tabs` wiring, which is
  lazy (`on_change="rerun"` plus `.open`), so the playground makes no network calls while you're
  typing in the explorer, and extra tabs are passed in by the entrypoint rather than known here.

One non-obvious detail: money rendered through Streamlit markdown is escaped as `\$` via the `md()`
and `usd()` helpers in `core/playground.py`. Streamlit reads a pair of unescaped `$` in one string as
inline LaTeX, which silently turns a caption full of dollar amounts into an equation. The explorer's
cards are exempt because their numbers sit inside `<div>` blocks, which CommonMark passes through
without parsing inline markdown.

## A leaderboard, if you want one

`core/scores.py` has the groundwork: `scorecard()` builds the row, `audit()` says whether to
believe it, and `Leaderboard` is the socket a backend plugs into. `NullLeaderboard` is what's
installed, so callers can check `.configured` and hide the UI rather than offering a board that
silently drops submissions.

**What a server can verify.** Settlement is deterministic — an option expiring on a date settles at
the underlying's real close, which `fetch_settlement_close` can re-fetch for any past date. So
every settled trade's P&L can be recomputed from scratch. The balance identity is checkable too.
Together those catch a book with free cash in it, a faked settlement cost, a trade settled before
it expired, and a premium that doesn't match its own per-share price:

```
! Balance doesn't add up. Balance drift of $99,700.00: cash is $298,800.00 but starting $200,000.00…
! MSFT Jan 16, 2026 400 call settled against $457.82, which costs $5,782.12 — the book recorded $0.00.
! AAPL Dec 17, 2027 200 put is recorded as settled but hasn't expired yet.
```

The settlement check is the one with teeth: the second line above passes the balance invariant
cleanly, because the book was edited consistently. Only re-deriving the close catches it.

**What no server can verify.** The premium someone claims to have collected. yfinance serves live
option quotes and historical *stock* closes — there is no historical option quote to check a claimed
fill against. A book asserting it sold a far-OTM put for $50 a share is arithmetically consistent
and unfalsifiable.

That limit has nothing to do with storing books in the browser, and accounts and a database would
not fix it. The only real fix is routing trades through the server as they happen, so the server
records the price instead of being told it. So: a fun board, not a contest.

**What's still needed to turn it on.** A `Leaderboard` implementation backed by an external
database (Community Cloud's disk is wiped on restart), reached via `st.connection` with credentials
in the app's secrets. Submission must stay explicit and opt-in — a book is private by construction
today, and quietly shipping it to a server the first time someone sells a put would undo the reason
it lives in the browser at all.

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
