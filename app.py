"""Options Selling Explorer — the public app, deployed to Streamlit Community Cloud.

Two tabs: an Explorer for eyeballing what cash-secured puts and covered calls
look like right now, and a Playground for practising selling them on paper.

Every visitor gets their own playground. The book lives in `st.session_state`
(see `core.stores.session_store`), so two people on the same server can't see or
spend each other's money, and nothing is written to disk. The trade-off is that
a book lasts as long as the browser session — that promise is made up front in
the banner rather than discovered when a balance disappears.

This entrypoint deliberately imports nothing from `bots/`. Keep it that way:
anything that trades on its own belongs in `bots_app.py`, which is not deployed.

Run with:  streamlit run app.py   (or ./app.sh start)
"""

from __future__ import annotations

from core import shell, stores

CAPTION = ("Live quotes from Yahoo Finance via yfinance. Paper trading only — the "
           "Playground tab simulates positions you sell here, and nothing in this "
           "app places a real trade.")

BANNER = ("This playground is yours alone — you start with $200,000 of pretend money "
          "and your positions are visible only in this browser session. Nothing is "
          "saved, so closing the tab or a server restart clears the book.")


def main() -> None:
    shell.run(
        stores.session_store(),
        page_title="Options Selling Explorer",
        title="📈 Options Selling Explorer",
        caption=CAPTION,
        banner=BANNER,
    )


if __name__ == "__main__":
    main()
