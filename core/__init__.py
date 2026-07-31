"""Everything both apps share.

This package is the whole of the public app: market data, the paper-trading
book, the explorer tab and the playground tab. It imports nothing from `bots`,
which is what makes the split work — `app.py` deploys to Streamlit Cloud with
the bot layer absent, and `bots_app.py` runs the same UI locally with an extra
tab bolted on.

The one thing that differs between the two deployments is where the book lives.
See `core.stores`: the public app gives every visitor their own in-memory book,
the local app keeps one on disk so bots can trade against it across restarts.
"""
