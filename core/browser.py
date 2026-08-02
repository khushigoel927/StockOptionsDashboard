"""Mirroring the paper book into the visitor's own browser storage.

Streamlit Community Cloud has no durable disk and one server for everyone, so
there is nowhere on the server a public visitor's book could live. `localStorage`
is the one place that is genuinely theirs: it survives closing the tab and every
app restart, it never leaves their machine, and it costs nothing to operate.

The exchange with the browser is deliberately tiny — one JS-only component,
mounted invisibly, with a two-phase protocol:

  * `write` is None → Python has nothing yet. The component reads storage and
    reports it back via the `stored` state key, which triggers one more rerun.
    This is the hydration handshake, and it's why a visitor's first render is
    gated behind a "restoring" message: without the gate they'd see a fresh
    $200,000 book for a moment and then watch it be replaced, which looks
    exactly like data loss.
  * `write` is a string → Python owns the book now. The component mirrors it
    into storage whenever it differs.

Storage can fail for reasons that are nobody's fault — Safari private browsing
throws on write, and a full quota throws too — so failures come back on the
`error` key and get surfaced rather than swallowed.
"""

from __future__ import annotations

import streamlit as st

# Versioned: if the book schema ever changes shape in a way `_migrate` can't
# handle, bump this rather than trying to reinterpret what's already out there.
STORAGE_KEY = "options-playground/book/v1"

_JS = """\
const KEY = "options-playground/book/v1"

export default function (component) {
  const { data, setStateValue } = component
  const write = data && typeof data.write === "string" ? data.write : null

  if (data && data.clear) {
    try { window.localStorage.removeItem(KEY) } catch (e) {}
    return
  }

  if (write === null) {
    // Hydration: tell Python what's already here. "" means nothing is stored,
    // which is different from null (the browser hasn't answered yet).
    let stored = ""
    try { stored = window.localStorage.getItem(KEY) || "" } catch (e) { stored = "" }
    setStateValue("stored", stored)
    return
  }

  // Steady state: keep storage in step with the book Python is holding. The
  // read-before-write keeps us from touching storage on reruns that changed
  // nothing, which is most of them.
  try {
    if (window.localStorage.getItem(KEY) !== write) {
      window.localStorage.setItem(KEY, write)
    }
    setStateValue("error", "")
  } catch (e) {
    setStateValue("error", String((e && e.message) || e))
  }
}
"""

# Registered once at import, never inside a function — re-registering the same
# name is a documented way to get confusing behaviour.
_SYNC = st.components.v2.component("local_book_storage", js=_JS)

SYNC_KEY = "book_sync"


class Reply:
    """What the browser said this run.

    `stored` is None until the browser has answered at all — distinct from "",
    which means it answered and had nothing saved.
    """

    def __init__(self, stored: str | None, error: str | None) -> None:
        self.stored = stored
        self.error = error or None

    @property
    def answered(self) -> bool:
        return self.stored is not None


def sync(*, write: str | None, clear: bool = False, key: str = SYNC_KEY) -> Reply:
    """Mount the storage component and report what the browser said."""
    result = _SYNC(
        key=key,
        data={"write": write, "clear": clear},
        on_stored_change=lambda: None,
        on_error_change=lambda: None,
    )
    return Reply(getattr(result, "stored", None), getattr(result, "error", None))


def forget(key: str = SYNC_KEY) -> None:
    """Erase the stored book from this browser."""
    sync(write=None, clear=True, key=f"{key}_clear")
