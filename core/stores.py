"""Where the paper-trading book lives — the one thing the two apps disagree on.

`app.py` (public, deployed) uses `session_store`; `bots_app.py` (local) uses
`shared_store`. Nothing else in `core` decides this, so a renderer can't
accidentally reach for the wrong one: it gets handed a Store and doesn't care.
"""

from __future__ import annotations

import streamlit as st

from . import paper

SESSION_KEY = "paper_store"


def session_store(starting_balance: float = paper.DEFAULT_STARTING_BALANCE) -> paper.Store:
    """A private, in-memory book per browser session — the public deployment.

    Deliberately NOT `@st.cache_resource`, which is process-global: on a shared
    server that would hand every visitor the same balance and let them settle
    each other's positions. Session state is the correct scope here precisely
    because it's the scope that isolates strangers from one another.

    The cost is that a book dies with its session (and with every app restart),
    which is why nothing is written to disk: a file that only one visitor could
    ever load back is worse than no file at all.
    """
    store = st.session_state.get(SESSION_KEY)
    if store is None:
        store = paper.Store(paper.NullBackend(starting_balance))
        st.session_state[SESSION_KEY] = store
    return store


@st.cache_resource(show_spinner=False)
def shared_store() -> paper.Store:
    """One disk-backed book per server process — the local, single-operator app.

    Process-global on purpose: the bots and every browser tab have to mutate the
    same book, or a bot's fills would be invisible to the UI and the next save
    from either side would clobber the other.

    Only safe because that deployment has exactly one user. Never wire this into
    `app.py`.
    """
    return paper.Store(paper.FileBackend(paper.book_path()))
