"""Keeperhub Swap Agent Dashboard.

Custom Streamlit dashboard for the keeperhub_swap_agent strategy. Loaded by the
hosted platform's dashboard image and by ``almanak dashboard``
locally — both call ``render_custom_dashboard()`` with the same
arguments.

Wired to the framework TA template renderer (``render_ta_dashboard``),
which owns the title, the strategy header, and the three audit
sections (PnL / Cost Stack / Trade Tape). Do NOT wrap it with
``st.title(...)`` or extra section helpers — that double-renders.
"""

from typing import Any

from almanak.framework.dashboard.templates import (
    get_rsi_config,
    prepare_ta_session_state,
    render_ta_dashboard,
)


def render_custom_dashboard(
    deployment_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    config = get_rsi_config(
        period=int(strategy_config.get("rsi_period", 14)),
        overbought=float(strategy_config.get("rsi_overbought", 70)),
        oversold=float(strategy_config.get("rsi_oversold", 30)),
    )
    config.base_token = str(strategy_config.get("base_token", config.base_token))
    config.quote_token = str(strategy_config.get("quote_token", config.quote_token))
    config.chain = str(strategy_config.get("chain", config.chain))
    config.protocol = str(strategy_config.get("protocol", config.protocol))

    session_state = prepare_ta_session_state(
        api_client,
        session_state=session_state,
        config=config,
        deployment_id=deployment_id,
    )

    render_ta_dashboard(deployment_id, strategy_config, session_state, config)
