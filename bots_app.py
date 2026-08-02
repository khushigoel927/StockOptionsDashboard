"""The local app — same Explorer and Playground, plus a tab for trading bots.

Not deployed. It differs from `app.py` in exactly one consequential way: the
book is process-global and persisted to `paper_book.json` (see
`core.stores.shared_store`), because a bot's fills have to outlive the browser
session that happened to be open when they were made.

That also means it is a single-operator app. Everyone who opens it shares one
balance, which is fine on localhost and would be a bug in public.

The Bots tab is a placeholder — see `bots/__init__.py` for the seam a strategy
plugs into.

Run with:  streamlit run bots_app.py   (or ./app.sh start bots)
"""

from __future__ import annotations

import streamlit as st

from core import paper, shell
from core.explorer import Ctx
from core import stores

CAPTION = ("Live quotes from Yahoo Finance via yfinance. Paper trading only — nothing "
           "here places a real trade. Local build: the book is shared and saved to disk.")


def render_bots(ctx: Ctx) -> None:
    st.header("Trading bots", anchor="bots", divider="gray")
    st.caption("Strategies that read the market and trade the same paper book the "
               "Playground tab is showing.")

    st.info("No bots are implemented yet. This tab is the mounting point — a bot "
            "reads quotes from `core.market` and trades through "
            "`store.mutate(paper.sell_position, ...)`, which is the same path the "
            "Sell buttons use.", icon=":material/robot_2:")

    book = ctx.store.book
    st.caption(f"Book: `{ctx.store.backend.label}` · {len(book['open'])} open · "
               f"{len(book['history'])} closed · available "
               f"\\${paper.available_cash(book):,.2f}")


def main() -> None:
    shell.run(
        stores.shared_store,
        page_title="Options Selling Explorer (local)",
        title="📈 Options Selling Explorer · local",
        caption=CAPTION,
        extra_tabs=[shell.ExtraTab("Bots", render_bots)],
    )


if __name__ == "__main__":
    main()
