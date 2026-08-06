"""
V0 ground-station dashboard (GS-001 through GS-005).

Connects to simulator/run_simulator.py over the local TCP link, displays live
telemetry, plots time-series data, shows mode/fault status, and sends telecommands.

Run: streamlit run ground-station/dashboard.py
(Start the simulator first: python simulator/run_simulator.py)
"""

import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from link import GroundLink, proto  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

st.set_page_config(page_title="CubeSAT ground station", layout="wide")


@st.cache_resource
def get_link(host: str, port: int) -> GroundLink:
    return GroundLink(host, port, csv_dir=DATA_DIR)


with st.sidebar:
    st.subheader("Link")
    host = st.text_input("Host", value="127.0.0.1", key="host")
    port = st.number_input("Port", value=5555, min_value=1, max_value=65535, key="port")
    link = get_link(host, int(port))
    st.caption(f"Logging to {link.csv_path}" if link.csv_path else "CSV logging not started")

    st.divider()
    st.subheader("Telemetry rate")
    rate = st.number_input(
        "Rate (Hz)", value=1.0, min_value=0.5, max_value=10.0, step=0.5, key="rate_input",
    )
    if st.button("Set rate", width="stretch"):
        link.send_command(proto.CommandId.SET_TELEMETRY_RATE, param=float(rate))


MODE_COLOR = {
    proto.Mode.BOOT: "gray",
    proto.Mode.NOMINAL: "green",
    proto.Mode.SAFE: "red",
    proto.Mode.TEST: "blue",
}


@st.fragment(run_every="1s")
def live_dashboard():
    snap = link.snapshot()
    latest = snap["latest"]
    history = snap["history"]

    if not snap["connected"]:
        st.warning(
            f"Not connected to {host}:{port} ({snap['connect_error'] or 'connecting...'}). "
            "Start the simulator: `python simulator/run_simulator.py`"
        )
        return

    if latest is None:
        st.info("Connected, waiting for the first telemetry packet...")
        return

    mode = proto.Mode(latest.mode)
    active_faults = [f.name for f in proto.FaultFlag if f != proto.FaultFlag.NONE and latest.fault_flags & f]
    unhealthy = [h.name for h in proto.HealthFlag if h not in (proto.HealthFlag.NONE, proto.HealthFlag.ALL_OK) and not (latest.health_flags & h)]

    st.markdown(f"### Mode: :{MODE_COLOR[mode]}[{mode.name}]")
    if active_faults:
        st.error("Active faults: " + ", ".join(active_faults))
    else:
        st.success("No active faults")
    if unhealthy:
        st.warning("Unhealthy sensors: " + ", ".join(unhealthy))

    df = pd.DataFrame([vars(p) for p in history])
    df["t_s"] = (df["timestamp_ms"] - df["timestamp_ms"].iloc[0]) / 1000.0

    with st.container(horizontal=True):
        st.metric("Temperature", f"{latest.temp_c:.1f} C", border=True,
                   chart_data=df["temp_c"].tolist(), chart_type="line")
        st.metric("Bus voltage", f"{latest.bus_voltage_v:.2f} V", border=True,
                   chart_data=df["bus_voltage_v"].tolist(), chart_type="line")
        st.metric("Bus current", f"{latest.bus_current_a:.2f} A", border=True,
                   chart_data=df["bus_current_a"].tolist(), chart_type="line")
        st.metric("Uptime", f"{latest.uptime_s} s", border=True)
        st.metric("Seq #", latest.seq_num, border=True)

    st.caption(
        f"cmds rx={latest.cmd_rx_count} accepted={latest.cmd_accept_count} "
        f"rejected={latest.cmd_reject_count} corrupted={latest.corrupted_rx_count}"
    )

    with st.container(border=True):
        st.subheader("Temperature")
        st.line_chart(df, x="t_s", y="temp_c")

    col1, col2 = st.columns(2)
    with col1:
        with st.container(border=True):
            st.subheader("Acceleration")
            st.line_chart(df, x="t_s", y=["accel_x", "accel_y", "accel_z"])
    with col2:
        with st.container(border=True):
            st.subheader("Angular rate")
            st.line_chart(df, x="t_s", y=["gyro_x", "gyro_y", "gyro_z"])

    col3, col4 = st.columns(2)
    with col3:
        with st.container(border=True):
            st.subheader("Bus voltage")
            st.line_chart(df, x="t_s", y="bus_voltage_v")
    with col4:
        with st.container(border=True):
            st.subheader("Bus current")
            st.line_chart(df, x="t_s", y="bus_current_a")

    with st.container(border=True):
        st.subheader("Command console")
        with st.container(horizontal=True):
            if st.button("Ping"):
                link.send_command(proto.CommandId.PING)
            if st.button("Get status"):
                link.send_command(proto.CommandId.GET_STATUS)
            if st.button("Enter SAFE mode"):
                link.send_command(proto.CommandId.ENTER_SAFE_MODE)
            if st.button("Exit SAFE mode"):
                link.send_command(proto.CommandId.EXIT_SAFE_MODE)
            if st.button("Reset faults"):
                link.send_command(proto.CommandId.RESET_FAULTS)
            if st.button("Request log"):
                link.send_command(proto.CommandId.REQUEST_LOG)
            if st.button("Enter TEST mode"):
                link.send_command(proto.CommandId.ENABLE, param=1)
            if st.button("Exit TEST mode"):
                link.send_command(proto.CommandId.DISABLE, param=1)

        log = snap["command_log"]
        if log:
            st.dataframe(pd.DataFrame(log), hide_index=True, width="stretch")
        else:
            st.caption("No commands sent yet.")


live_dashboard()
