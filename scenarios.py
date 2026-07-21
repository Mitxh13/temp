"""
scenarios.py
------------
A reference catalog of common spacecraft operational scenarios, and
deterministic (rule-based, NOT AI) logic that flags which of them the
current telemetry snapshot might relate to.

Why rule-based and not "ask the LLM which scenario this is": a 3B model
guessing freely at a fault classification is exactly the kind of thing
that can sound confident and be wrong. So this file does the matching in
plain Python from things that are actually in the data (which PID tags an
event's condition/guard reference, plus a keyword fallback on its name/
description). The LLM is only ever allowed to narrate what this file
already matched -- see the SYSTEM_PROMPT in report_generator.py.

*** THE ONE THING YOU'LL LIKELY NEED TO EDIT ***
PID naming conventions differ per spacecraft/mission. PID_PREFIX_MAP below
is a heuristic guess at what your prefixes mean (PWR -> power, AOC ->
attitude/orbit control, TCP -> guessed as comms, etc.) based on the sample
PIDs seen so far. Check it against your actual PID dictionary/ICD and
correct anything that's wrong for your spacecraft -- nothing else in this
file needs to change to do that.
"""

import re
from typing import Any, Dict, List

# --------------------------------------------------------------------------- #
# The catalog -- 10 common spacecraft operational scenarios.
# Standard subsystem/ops categories (EPS, AOCS, TCS, TT&C, propulsion,
# payload, OBDH, safe mode, ground segment) that apply broadly across
# spacecraft types. Add/remove/reword freely for your mission.
# --------------------------------------------------------------------------- #
SCENARIOS: List[Dict[str, str]] = [
    {
        "id": "EPS",
        "name": "EPS (Electrical Power System) anomaly",
        "description": "Power bus over/under-voltage, solar array underperformance, or power distribution fault.",
    },
    {
        "id": "BATTERY",
        "name": "Battery / eclipse power management issue",
        "description": "Battery depth-of-discharge exceeded, charge/discharge anomaly, or eclipse-transition power transient.",
    },
    {
        "id": "AOCS",
        "name": "AOCS/ADCS (attitude & orbit control) anomaly",
        "description": "Attitude drift, pointing error, reaction wheel saturation, or sensor disagreement.",
    },
    {
        "id": "THERMAL",
        "name": "Thermal control anomaly",
        "description": "Subsystem or payload over/under-temperature, heater or radiator fault.",
    },
    {
        "id": "COMMS",
        "name": "TT&C / communications anomaly",
        "description": "Loss of signal, link degradation, transponder fault, or ground-station handover issue.",
    },
    {
        "id": "PROPULSION",
        "name": "Propulsion system anomaly",
        "description": "Thruster misfire, fuel/propellant pressure out of range, or orbit maneuver failure.",
    },
    {
        "id": "PAYLOAD",
        "name": "Payload anomaly",
        "description": "Instrument fault, payload data corruption, or calibration drift.",
    },
    {
        "id": "OBDH",
        "name": "OBC / data-handling anomaly",
        "description": "Onboard computer reset, memory error, or telemetry gap/data loss.",
    },
    {
        "id": "SAFEMODE",
        "name": "Safe-mode entry / fault-protection response",
        "description": "The spacecraft's fault-protection logic has autonomously reacted to a detected fault.",
    },
    {
        "id": "GROUND_LINK",
        "name": "Ground segment / command link issue",
        "description": "Command rejection, uplink failure, or ground-station scheduling conflict.",
    },
]

_SCENARIO_BY_ID = {s["id"]: s for s in SCENARIOS}

# --------------------------------------------------------------------------- #
# PID prefix -> scenario id. *** EDIT THIS for your spacecraft. ***
# Matching is case-insensitive on the letters before the first digit, e.g.
# "PWR05013" -> "PWR", "AOC00811" -> "AOC".
# --------------------------------------------------------------------------- #
PID_PREFIX_MAP: Dict[str, str] = {
    "PWR": "EPS",
    "SLR": "EPS",
    "BUS": "EPS",
    "BAT": "BATTERY",
    "AOC": "AOCS",
    "ADC": "AOCS",
    "GYR": "AOCS",
    "RWA": "AOCS",
    "STR": "AOCS",   # star tracker
    "TMP": "THERMAL",
    "THM": "THERMAL",
    "HTR": "THERMAL",
    "COM": "COMMS",
    "TCP": "COMMS",  # guess: "telecommand processor" -- verify for your ICD
    "RF": "COMMS",
    "PRO": "PROPULSION",
    "THR": "PROPULSION",
    "FUEL": "PROPULSION",
    "PAY": "PAYLOAD",
    "INS": "PAYLOAD",
    "OBC": "OBDH",
    "MEM": "OBDH",
    "CMD": "GROUND_LINK",
}

# Secondary signal: keywords in EventName/EventDescription/Action, for when
# a PID prefix alone doesn't give a confident match.
KEYWORD_MAP: Dict[str, str] = {
    "power": "EPS", "voltage": "EPS", "solar": "EPS",
    "battery": "BATTERY", "eclipse": "BATTERY", "charge": "BATTERY",
    "attitude": "AOCS", "pointing": "AOCS", "wheel": "AOCS", "gyro": "AOCS",
    "thermal": "THERMAL", "temperature": "THERMAL", "heater": "THERMAL",
    "comm": "COMMS", "signal": "COMMS", "link": "COMMS", "transponder": "COMMS",
    "thruster": "PROPULSION", "propellant": "PROPULSION", "fuel": "PROPULSION", "maneuver": "PROPULSION",
    "payload": "PAYLOAD", "instrument": "PAYLOAD", "calibration": "PAYLOAD",
    "reset": "OBDH", "memory": "OBDH", "onboard computer": "OBDH",
    "safe mode": "SAFEMODE", "fault protection": "SAFEMODE",
    "command": "GROUND_LINK", "uplink": "GROUND_LINK", "ground station": "GROUND_LINK",
}

_PID_RE = re.compile(r'PID\("([A-Za-z0-9_]+)"\)')
_PREFIX_RE = re.compile(r'[A-Za-z]+')


def _extract_pid_tags(text: str) -> List[str]:
    if not text:
        return []
    return _PID_RE.findall(text)


def _prefix_of(pid_tag: str) -> str:
    m = _PREFIX_RE.match(pid_tag)
    return m.group(0).upper() if m else ""


def match_scenarios(
    analyses: List[Dict[str, Any]], raw_events: List[Any]
) -> List[Dict[str, Any]]:
    """
    Deterministic, rule-based matching -- no AI involved.

    For each event, look at the PID tags referenced in its EventCondition
    and GuardCondition (plus a keyword fallback on its name/description/
    action) and map them to entries in the SCENARIOS catalog above.

    `analyses` = output of analyze_event() for each event, in the same
    order as `raw_events` = the corresponding EventDetail objects.

    Returns one dict per matched scenario id (catalog order), each listing
    which events triggered it, why, and whether that event currently looks
    "active" (worth a second look) vs just a subsystem being mentioned.
    """
    hits: Dict[str, Dict[str, Any]] = {}

    for a, ev in zip(analyses, raw_events):
        matched_ids = set()
        basis_by_id: Dict[str, List[str]] = {}

        tags = _extract_pid_tags(ev.event_condition) + _extract_pid_tags(ev.guard_condition)
        for tag in tags:
            sid = PID_PREFIX_MAP.get(_prefix_of(tag))
            if sid:
                matched_ids.add(sid)
                basis_by_id.setdefault(sid, []).append('PID "{0}"'.format(tag))

        haystack = " ".join(
            [ev.event_name or "", ev.event_description or "", ev.action or ""]
        ).lower()
        for kw, sid in KEYWORD_MAP.items():
            if kw in haystack:
                matched_ids.add(sid)
                basis_by_id.setdefault(sid, []).append('keyword "{0}"'.format(kw))

        currently_active = (
            a["condition_state"] == "TRUE"
            or a["action_sts"].strip().upper() not in ("NOT_INITIATED", "")
            or not a["health_ok"]
        )

        for sid in matched_ids:
            entry = hits.setdefault(
                sid,
                {
                    "id": sid,
                    "name": _SCENARIO_BY_ID[sid]["name"],
                    "description": _SCENARIO_BY_ID[sid]["description"],
                    "events": [],
                },
            )
            entry["events"].append(
                {
                    "event_id": a["event_id"],
                    "event_name": a["event_name"],
                    "basis": ", ".join(sorted(set(basis_by_id.get(sid, [])))),
                    "currently_active": currently_active,
                }
            )

    # stable, catalog-defined order; only scenarios that actually matched
    return [hits[s["id"]] for s in SCENARIOS if s["id"] in hits]


def format_scenario_catalog() -> str:
    """One line per scenario -- a compact, self-contained reference appendix."""
    return "\n".join(
        "{0}. {1} - {2}".format(i, s["name"], s["description"])
        for i, s in enumerate(SCENARIOS, 1)
    )


def format_matched_scenarios(matches: List[Dict[str, Any]]) -> str:
    if not matches:
        return "No known operational-scenario pattern (see catalog below) matched this snapshot."
    blocks = []
    for m in matches:
        lines = [m["name"]]
        for e in m["events"]:
            flag = "  <-- currently active" if e["currently_active"] else ""
            lines.append(
                '    {0} ("{1}") - matched on {2}{3}'.format(
                    e["event_id"], e["event_name"], e["basis"], flag
                )
            )
        blocks.append("\n".join(lines))
    return "\n".join(blocks)
