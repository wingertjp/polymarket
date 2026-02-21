"""
Polymarket BTC Up/Down — chart mode.

Interactive browser viewer for recorded sessions.
Runs a Plotly Dash app at http://localhost:8050.

Usage:
  python main.py chart
  python chart.py
"""

import json
from pathlib import Path

import dash
from dash import dcc, html, Input, Output
import plotly.graph_objects as go
from plotly.subplots import make_subplots

RECORDINGS_DIR = Path(__file__).parent / "recordings"


def _list_recordings() -> list[dict]:
    """Return recordings sorted newest first as Dropdown options."""
    files = sorted(RECORDINGS_DIR.glob("*.jsonl"), reverse=True)
    options = []
    for f in files:
        name = f.stem  # e.g. 2026-02-21T14-30-00_btc-updown-5m-1740150600
        options.append({"label": name, "value": str(f)})
    return options


def _load_ticks(path: str) -> list[dict]:
    ticks = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                ticks.append(json.loads(line))
    return ticks


def _build_figure(ticks: list[dict]) -> go.Figure:
    if not ticks:
        fig = go.Figure()
        fig.update_layout(title="No data")
        return fig

    t0 = ticks[0]["ts"]
    elapsed   = [t["ts"] - t0 for t in ticks]
    up_mid    = [t.get("up_mid")   for t in ticks]
    down_mid  = [t.get("down_mid") for t in ticks]
    btc       = [t.get("btc")      for t in ticks]

    # btc_open: use first non-null value across all ticks
    btc_open = next((t.get("btc_open") for t in ticks if t.get("btc_open")), None)

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(
        go.Scatter(
            x=elapsed, y=up_mid,
            name="Up mid", line=dict(color="green", width=1.5),
            mode="lines",
        ),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=elapsed, y=down_mid,
            name="Down mid", line=dict(color="red", width=1.5),
            mode="lines",
        ),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=elapsed, y=btc,
            name="BTC/USDT", line=dict(color="orange", width=1.5),
            mode="lines",
        ),
        secondary_y=True,
    )
    if btc_open is not None:
        fig.add_trace(
            go.Scatter(
                x=[elapsed[0], elapsed[-1]], y=[btc_open, btc_open],
                name=f"BTC open ({btc_open:,.2f})",
                line=dict(color="orange", width=1, dash="dot"),
                mode="lines",
            ),
            secondary_y=True,
        )

    fig.update_layout(
        template="plotly_dark",
        margin=dict(l=60, r=60, t=50, b=50),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    fig.update_xaxes(title_text="Elapsed (s)")
    fig.update_yaxes(title_text="Probability [0–1]", range=[0, 1], secondary_y=False)
    fig.update_yaxes(title_text="BTC price (USD)", secondary_y=True)

    return fig


def run_chart_mode() -> None:
    app = dash.Dash(__name__, title="Polymarket chart")

    def _layout():
        recordings = _list_recordings()
        return html.Div(
            style={"fontFamily": "monospace", "backgroundColor": "#111", "color": "#eee",
                   "minHeight": "100vh", "padding": "20px"},
            children=[
                html.H3("Polymarket BTC Up/Down — session browser",
                        style={"marginBottom": "16px"}),
                dcc.Dropdown(
                    id="session-dropdown",
                    options=recordings,
                    value=recordings[0]["value"] if recordings else None,
                    clearable=False,
                    style={"color": "#111", "marginBottom": "16px"},
                ),
                dcc.Graph(
                    id="main-chart",
                    style={"height": "70vh"},
                    config={"displayModeBar": True},
                ),
            ],
        )

    app.layout = _layout

    @app.callback(
        Output("main-chart", "figure"),
        Input("session-dropdown", "value"),
    )
    def update_chart(path: str | None) -> go.Figure:
        if not path:
            return go.Figure()
        try:
            ticks = _load_ticks(path)
            return _build_figure(ticks)
        except Exception as e:
            fig = go.Figure()
            fig.update_layout(title=f"Error loading {path}: {e}")
            return fig

    print("Chart viewer running at http://localhost:8050")
    app.run(debug=False, host="localhost", port=8050)


if __name__ == "__main__":
    run_chart_mode()
