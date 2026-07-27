# 📈 Options Selling Explorer

A small Streamlit dashboard for eyeballing **cash-secured puts** and **covered calls** with live
option-chain data from Yahoo Finance.

It is not a position tracker and it is not connected to a broker. Each row is a scratch card that
answers one question: *what would this contract look like right now?* Rows live in
`st.session_state` and disappear when the browser session ends.

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

Editing a card's strike or expiration re-prices it immediately. **🔄 Refresh all** clears the data
cache and re-pulls every row.

## Requirements

- Python 3.10+ (the code uses `X | None` type syntax)
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
app.py            the whole dashboard — data access, quote math, and UI
app.sh            start/stop/restart/status/logs wrapper around Streamlit
requirements.txt  streamlit, yfinance, pandas
howToRun.txt      the app.sh cheat sheet
```

Inside `app.py`:

- **Data access** — `fetch_expirations`, `fetch_spot`, `fetch_chain`, `fetch_strikes`, each wrapped
  in `st.cache_data` with a short TTL (60s for quotes and chains, 15m for expiration lists). Every
  provider failure is normalized into a `DataError` so a broken row shows a message instead of a
  traceback.
- **Quote math** — `price_contract` finds the nearest listed strike and computes premium, cash
  required, and the move needed for the buyer to exercise. `liquidity` produces the badge.
- **UI** — `render_add_form` and `render_card`, with the puts and calls sections stacked on one
  scrollable page.

## Caveats

- Yahoo Finance data is delayed and occasionally missing or wrong. Treat everything here as a
  rough sketch, not a quote you can trade against.
- Premium uses the **bid**, i.e. what you'd get selling into the market — no mid-price optimism.
- No commissions, fees, assignment risk, or early-exercise modeling.
- Nothing is persisted: rows are gone on refresh of the browser session.

## Disclaimer

For educational and exploratory use only. This is not financial advice, and nothing in this app
places a trade.
