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
from timeline import build_timeline, flag_authority, summarise  # noqa: E402

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

    # G2: an out-of-range mode from a buggy firmware build must not take the
    # dashboard down with it.
    try:
        mode = proto.Mode(latest.mode)
        mode_label, mode_colour = mode.name, MODE_COLOR[mode]
    except ValueError:
        mode_label, mode_colour = f"UNKNOWN ({latest.mode})", "orange"
    active_faults = [f.name for f in proto.FaultFlag if f != proto.FaultFlag.NONE and latest.fault_flags & f]
    unhealthy = [h.name for h in proto.HealthFlag if h not in (proto.HealthFlag.NONE, proto.HealthFlag.ALL_OK) and not (latest.health_flags & h)]

    st.markdown(f"### Mode: :{mode_colour}[{mode_label}]")
    if active_faults:
        # Split by AUTHORITY, not just by "is it set". Rendering an advisory
        # anomaly identically to a flag that can command SAFE made the
        # architecture's central boundary invisible to the operator -- the
        # cheapest correction available, and it needs no wire change.
        commanding = [f for f in active_faults
                      if flag_authority(getattr(proto.FaultFlag, f)) == "commands SAFE"]
        authorising = [f for f in active_faults
                       if flag_authority(getattr(proto.FaultFlag, f)) == "can authorise recovery"]
        advisory = [f for f in active_faults
                    if flag_authority(getattr(proto.FaultFlag, f)) == "advisory only"]
        informational = [f for f in active_faults
                         if f not in commanding + authorising + advisory]
        if commanding:
            st.error("Commands SAFE: " + ", ".join(commanding))
        if authorising:
            st.warning("Can authorise recovery: " + ", ".join(authorising))
        if advisory:
            st.info("Advisory only (cannot command SAFE or any action): " + ", ".join(advisory))
        if informational:
            st.caption("Informational: " + ", ".join(informational))
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
    # G3: link quality measured at the GROUND end. Packet loss is one of the
    # five numbers this project must produce, and read_packet's corruption
    # signal was previously discarded without ever being counted.
    st.caption(
        f"downlink: {snap['corrupted_rx_count']} corrupted frame(s), "
        f"{snap['decode_error_count']} decode error(s)"
        + (f" -- last: {snap['last_decode_error']}" if snap['last_decode_error'] else "")
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
        st.subheader("FDIR event timeline")
        st.caption("Reconstructed by diffing consecutive telemetry packets -- this "
                   "narrative was already on the wire and previously discarded.")
        events = build_timeline(history)
        if events:
            summary = summarise(events)
            if summary["flag_to_safe_s"] is not None:
                st.caption(f"first fault `{summary['first_fault']}` at "
                           f"t={summary['first_fault_t_s']:.2f}s; SAFE entered "
                           f"{summary['flag_to_safe_s']:.2f}s later")
            icon = {"critical": ":material/error:", "warning": ":material/warning:",
                    "recovery": ":material/check_circle:", "info": ":material/info:"}
            rows = [{"t (s)": round(e.t_s, 2),
                     "": icon.get(e.severity, ":material/info:"),
                     "event": e.label,
                     "kind": e.kind,
                     "authority": e.authority or "-"}
                    for e in events[-40:]]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            st.caption("No transitions observed yet.")

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
