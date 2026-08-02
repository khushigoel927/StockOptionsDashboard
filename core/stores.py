"""Where the paper-trading book lives — the one thing the two apps disagree on.

`app.py` (public, deployed) uses `session_store`; `bots_app.py` (local) uses
`shared_store`. Nothing else in `core` decides this, so a renderer can't
accidentally reach for the wrong one: it gets handed a Store and doesn't care.
"""

from __future__ import annotations

import streamlit as st

from . import browser, paper

SESSION_KEY = "paper_store"


class BrowserBackend(paper.Backend):
    """A book kept in the visitor's own `localStorage`.

    The odd one out among the backends: `save()` does nothing. A browser write
    can't happen synchronously inside `Store.mutate` — it needs the component to
    render — so the actual mirroring is done by `session_store` on the next
    render pass, from whatever the book holds by then.

    That's a deliberate trade rather than an oversight. The alternative is
    threading a render step through `mutate`, which would put UI plumbing inside
    the one module that has stayed free of it. The cost is that a change made
    late in a run reaches storage one rerun later, which is invisible in
    practice because every mutation in this app is followed by a rerun.
    """

    label = "your browser"
    erasable = True

    def __init__(self, book: dict, warning: str | None = None) -> None:
        self._book = book
        self._warning = warning

    def load(self) -> tuple[dict, str | None, bool]:
        return self._book, self._warning, False

    def save(self, book: dict) -> None:
        pass  # see the class docstring: the mirror happens at render time

    def erase(self) -> None:
        browser.forget()


def _restore(stored: str) -> paper.Store:
    """Build a Store from whatever the browser handed back."""
    if not stored:
        return paper.Store(BrowserBackend(paper.new_book()))

    book, warning = paper.parse_book(stored)
    drift = paper.check_invariant(book)
    if drift:
        # Storage the user could have edited, so say so rather than trusting it
        # silently — and rather than throwing away what may be real trades.
        warning = " ".join(filter(None, [warning, drift]))
    return paper.Store(BrowserBackend(book, warning))


def session_store() -> paper.Store:
    """The public deployment's book: private to this visitor, kept in their browser.

    Deliberately NOT `@st.cache_resource`, which is process-global: on a shared
    server that would hand every visitor the same balance and let them settle
    each other's positions. Session state is the correct scope here precisely
    because it's the scope that isolates strangers from one another.

    Calls `st.stop()` while waiting for the browser to report what it has, so
    callers must render anything they want visible during that pause *before*
    calling this.
    """
    store = st.session_state.get(SESSION_KEY)

    # Exactly one mount per run, whichever phase we're in. Deciding what to
    # write *before* mounting is what keeps that true: mounting to read and then
    # again to write would be two elements sharing one key, which Streamlit
    # rejects — and the run where hydration finishes is the run that does both.
    write = paper.dump_book(store.book, compact=True) if store is not None else None
    reply = browser.sync(write=write)

    if store is None:
        if not reply.answered:
            # One extra rerun, by construction — the component can't answer
            # until it has mounted. Gate rather than render a fresh book that
            # is about to be replaced.
            st.info("Restoring your playground from this browser…",
                    icon=":material/hourglass_top:")
            st.stop()
        # Nothing to mirror on this run: the book either came out of storage
        # already or is empty. The first change to it triggers a rerun, and that
        # run writes.
        store = _restore(reply.stored)
        st.session_state[SESSION_KEY] = store

    store.backend.status_error = (
        f"This browser wouldn't save your playground ({reply.error}). Private "
        "browsing and a full storage quota both do this. You can keep trading, "
        "but the book will be gone when you close the tab — use Save or load to "
        "download a copy."
    ) if reply.error else None
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
