"""The bot layer — local only, never imported by the deployed app.

Empty on purpose: the strategies aren't designed yet. What exists today is the
seam they'll plug into.

The seam
--------
A bot needs exactly two things, and both already exist without any bot-specific
plumbing:

  * `core.market` — quotes. Pure functions, no UI, raise `DataError` when a
    contract can't be priced.
  * `core.paper.Store` — the book. `store.mutate(paper.sell_position, ...)`
    takes a lock, applies the trade, and persists it. Every mutation returns
    `(ok, message)` and validates its own inputs, so a bot can't overdraw the
    account or book a position the reserve doesn't cover; it doesn't need to
    re-check what `paper.py` already enforces.

`bots_app.py` hands the Bots tab the same `Store` the Playground is rendering,
so a fill made here shows up there on the next rerun with no syncing.

Why the deployed app can't reach this
-------------------------------------
`app.py` imports `core` and nothing else. Streamlit Cloud runs `app.py`, so
whatever lands in this package is never loaded there, even though it's in the
same repository. Keep that one-way arrow: `bots` may import `core`, `core` must
never import `bots`.
"""
