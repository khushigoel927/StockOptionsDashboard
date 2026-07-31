"""The page both apps draw: title, sidebar, toolbar, Explorer + Playground tabs.

The only differences between the public deployment and the local one are passed
in — which Store to trade against, and any extra tabs. Keeping the chrome here
means a change to the header or the liquidity controls lands in both apps at
once, and neither entrypoint has to know how the other is deployed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

import streamlit as st

from . import explorer, paper, playground
from .explorer import Ctx


@dataclass(frozen=True)
class ExtraTab:
    """A tab bolted on by an entrypoint. `render` is called with the live Ctx."""
    label: str
    render: Callable[[Ctx], None]


def run(store: paper.Store, *, title: str, caption: str, page_title: str,
        extra_tabs: Sequence[ExtraTab] = (), banner: str | None = None) -> None:
    st.set_page_config(page_title=page_title, page_icon="📈", layout="wide")
    explorer.init_state()
    st.markdown(explorer.CSS, unsafe_allow_html=True)

    st.title(title)
    st.caption(caption)
    if banner:
        st.info(banner, icon=":material/info:")

    with st.sidebar:
        st.header("Liquidity thresholds")
        min_oi = st.number_input("Minimum open interest", min_value=0, value=100, step=25)
        min_volume = st.number_input("Minimum daily volume", min_value=0, value=10, step=5)
        st.caption("Rows below either threshold are badged **Thin**. A missing or "
                   "zero bid is always badged **No bid**.")

    ctx = Ctx(store=store, min_oi=int(min_oi), min_volume=int(min_volume))
    explorer.render_toolbar(ctx)

    # on_change="rerun" is what makes `.open` meaningful — without it Streamlit
    # runs *every* tab body on every rerun, so the playground's price fetches
    # would fire on each keystroke in the explorer.
    labels = ["Explorer", "Playground", *(t.label for t in extra_tabs)]
    tabs = st.tabs(labels, on_change="rerun", key="main_tab")
    explorer_tab, playground_tab, extras = tabs[0], tabs[1], tabs[2:]

    if explorer_tab.open:
        with explorer_tab:
            explorer.render_explorer(ctx)

    if playground_tab.open:
        with playground_tab:
            playground.render_playground(ctx.store, ctx.min_oi, ctx.min_volume)

    for tab, extra in zip(extras, extra_tabs):
        if tab.open:
            with tab:
                extra.render(ctx)
