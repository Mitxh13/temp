"""
report_generator.py
--------------------
Turns a validated TelemetryPayload into a human-readable report.

Design choice, and why:
  A 3B quantized model is small and WILL occasionally get arithmetic / boolean
  logic wrong if you ask it to "figure out" what the JSON means. So instead of
  handing the model raw JSON, we do all the actual logic (was the condition
  true, was the guard true, is monitoring healthy, are samples complete...)
  deterministically in plain Python first. The model's only job is to turn an
  already-correct list of facts into a readable paragraph. This keeps the
  report trustworthy even though the model is small, and it also means the
  tool still works (via `_fallback_narrative`) if the llama.cpp server is
  down -- it just skips the "nice prose" part.

Only stdlib is used to talk to llama.cpp (urllib), so this file does not
require httpx. If httpx becomes available on your machine later, swap
`call_llm`'s body for an httpx.Client call -- everything else stays the same.
"""

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from models import EventDetail, TelemetryPayload
from scenarios import format_matched_scenarios, format_scenario_catalog, match_scenarios

SYSTEM_PROMPT = (
    "You are a telemetry analyst writing the top-of-report situation summary "
    "for a spacecraft operator, based ONLY on the facts given below. "
    "Never invent numbers, PID tags, scenario names, or statuses that are not "
    "in the facts. Do not use markdown, headings, or bullet points -- plain "
    "prose only, 3 to 6 sentences. Mention how many events were analyzed, "
    "call out anything listed under 'Attention items' or 'Matched scenarios' "
    "by name, and give an overall health read. This summary sits above a "
    "longer report that already lists every event's exact numbers, so don't "
    "restate each event in detail -- give the operator the big picture first."
)


# --------------------------------------------------------------------------- #
# Deterministic analysis
# --------------------------------------------------------------------------- #

def _bool_list_state(values: List[bool]) -> str:
    """Collapse a list of sampled booleans into one state word."""
    if not values:
        return "UNKNOWN"
    if all(v is True for v in values):
        return "TRUE"
    if all(v is False for v in values):
        return "FALSE"
    return "MIXED"


def _logic_note(
    condition_state: str,
    guard_state: str,
    samples_complete: bool,
    samples_available: int,
    samples_expected: int,
) -> str:
    if condition_state == "TRUE" and guard_state == "TRUE":
        if not samples_complete:
            return (
                "the condition and the guard are both currently satisfied, but only "
                "{0} of {1} required samples have been collected so far, so the "
                "action has not triggered yet"
            ).format(samples_available, samples_expected)
        return "the condition and the guard are both currently satisfied, so the action is eligible to fire"
    if condition_state == "TRUE" and guard_state == "FALSE":
        return "the condition is currently satisfied but the guard condition is not, so the action is being held back"
    if condition_state == "TRUE" and guard_state == "UNKNOWN":
        return "the condition is currently satisfied, but the guard could not be evaluated (no sampled values)"
    if condition_state == "FALSE":
        return "the condition is not currently satisfied, so the action will not fire regardless of the guard"
    if condition_state == "MIXED":
        return "the condition gave mixed results across the sampled values, so its state is not fully settled"
    return "no sampled values were available yet to evaluate the condition"


def analyze_event(ev: EventDetail) -> Dict[str, Any]:
    """Compute all derived facts for one event. Pure, deterministic, no LLM."""
    condition_state = _bool_list_state(ev.event_condition_sts)
    guard_state = _bool_list_state(ev.guard_condition_sts)
    # Real captures show both "MON OK" and "MON_OK" for the same healthy
    # state, so normalize underscores to spaces before comparing.
    health_ok = (
        ev.event_monitoring_health.strip().upper().replace("_", " ") == "MON OK"
    )
    samples_complete = ev.cur_no_of_samples_available >= ev.no_of_samples

    return {
        "event_id": ev.event_id,
        "event_name": ev.event_name,
        "event_description": ev.event_description,
        "action": ev.action,
        "enabled": ev.enabled,
        "currently_monitored": ev.currently_monitored,
        "valid_event": ev.valid_event,
        "event_condition": ev.event_condition,
        "condition_state": condition_state,
        "guard_condition": ev.guard_condition,
        "guard_state": guard_state,
        "action_sts": ev.action_sts,
        "logic_note": _logic_note(
            condition_state,
            guard_state,
            samples_complete,
            ev.cur_no_of_samples_available,
            ev.no_of_samples,
        ),
        "monitoring_health": ev.event_monitoring_health,
        "health_ok": health_ok,
        "samples_available": ev.cur_no_of_samples_available,
        "samples_expected": ev.no_of_samples,
        "samples_complete": samples_complete,
        "retriggering_time_ms": ev.retriggering_time_ms,
    }


def _format_event_facts(a: Dict[str, Any]) -> str:
    lines = [
        'Event {0} - "{1}" ({2})'.format(
            a["event_id"], a["event_name"], a["event_description"]
        ),
        "  Action script       : {0}".format(a["action"]),
        "  Enabled / Monitored : {0} / {1}".format(
            a["enabled"], a["currently_monitored"]
        ),
        "  Event condition     : {0}  -> currently {1}".format(
            a["event_condition"], a["condition_state"]
        ),
        "  Guard condition     : {0}  -> currently {1}".format(
            a["guard_condition"], a["guard_state"]
        ),
        "  Action status       : {0} ({1})".format(a["action_sts"], a["logic_note"]),
        "  Monitoring health   : {0}{1}".format(
            a["monitoring_health"], "" if a["health_ok"] else "  <-- NOT OK"
        ),
        "  Samples             : {0} of {1} available{2}".format(
            a["samples_available"],
            a["samples_expected"],
            "" if a["samples_complete"] else "  <-- incomplete",
        ),
        "  Re-trigger interval : {0} ms ({1:.1f} s)".format(
            a["retriggering_time_ms"], a["retriggering_time_ms"] / 1000.0
        ),
    ]
    return "\n".join(lines)


def _attention_items(analyses: List[Dict[str, Any]]) -> List[str]:
    """
    Single-line, plain-English flags for events that are worth a human's
    attention -- pulled out so you don't have to read every event's full
    block to spot the ones that matter. Deterministic, same facts as above.
    """
    items = []
    for a in analyses:
        flags = []
        if not a["health_ok"]:
            flags.append("monitoring health is {0}".format(a["monitoring_health"]))
        if a["condition_state"] == "MIXED":
            flags.append("condition samples are mixed/unsettled")
        if a["guard_state"] == "MIXED":
            flags.append("guard samples are mixed/unsettled")
        if a["action_sts"].strip().upper() not in ("NOT_INITIATED", ""):
            flags.append("action status is {0}".format(a["action_sts"]))
        if (
            not a["samples_complete"]
            and a["condition_state"] == "TRUE"
            and a["guard_state"] == "TRUE"
        ):
            flags.append(
                "ready to trigger but waiting on samples ({0}/{1})".format(
                    a["samples_available"], a["samples_expected"]
                )
            )
        if flags:
            items.append(
                '{0} ("{1}"): {2}'.format(a["event_id"], a["event_name"], "; ".join(flags))
            )
    return items


def _at_a_glance(analyses: List[Dict[str, Any]], scenario_count: int) -> str:
    n = len(analyses)
    conditions_true = sum(1 for a in analyses if a["condition_state"] == "TRUE")
    guards_blocking = sum(1 for a in analyses if a["guard_state"] == "FALSE")
    actions_active = sum(
        1 for a in analyses if a["action_sts"].strip().upper() not in ("NOT_INITIATED", "")
    )
    health_issues = sum(1 for a in analyses if not a["health_ok"])
    incomplete = sum(1 for a in analyses if not a["samples_complete"])
    return "\n".join(
        [
            "Events analyzed             : {0}".format(n),
            "Conditions currently TRUE   : {0}".format(conditions_true),
            "Guards currently blocking   : {0}".format(guards_blocking),
            "Actions currently active    : {0}".format(actions_active),
            "Monitoring health issues    : {0}".format(health_issues),
            "Incomplete sample sets      : {0}".format(incomplete),
            "Scenarios flagged           : {0}".format(scenario_count),
        ]
    )



def _fallback_narrative(
    analyses: List[Dict[str, Any]],
    attention: List[str],
    scenario_matches: List[Dict[str, Any]],
) -> str:
    """Used only if the llama.cpp server can't be reached."""
    n = len(analyses)
    lines = [
        "{0} event(s) analyzed. This summary was generated without the "
        "language model, because the llama.cpp server could not be reached "
        "-- only the deterministic facts below were used.".format(n)
    ]
    if scenario_matches:
        names = ", ".join(m["name"] for m in scenario_matches)
        lines.append("Matched scenario patterns: {0}.".format(names))
    if attention:
        lines.append("Attention items:")
        lines.extend("  - {0}".format(item) for item in attention)
    else:
        lines.append("No attention items were flagged.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Prompt building
# --------------------------------------------------------------------------- #

def _build_user_prompt(
    source: str,
    generated_at: str,
    analyses: List[Dict[str, Any]],
    log_meta: Optional[Dict[str, Any]] = None,
    attention: Optional[List[str]] = None,
    scenario_matches: Optional[List[Dict[str, Any]]] = None,
) -> str:
    facts = "\n\n".join(_format_event_facts(a) for a in analyses)
    meta_lines = ""
    if log_meta:
        parts = []
        if log_meta.get("process"):
            parts.append("Process: {0}".format(log_meta["process"]))
        if log_meta.get("timetag"):
            parts.append("Log timestamp: {0}".format(log_meta["timetag"]))
        if log_meta.get("log_type"):
            parts.append("Log type: {0}".format(log_meta["log_type"]))
        if parts:
            meta_lines = "\n".join(parts) + "\n"

    attention_block = (
        "Attention items:\n" + "\n".join("- {0}".format(i) for i in attention)
        if attention
        else "Attention items: none flagged."
    )
    scenario_block = "Matched scenarios:\n" + format_matched_scenarios(scenario_matches or [])

    return (
        "Source: {0}\n"
        "{4}"
        "Report generated at: {1}\n"
        "Number of events: {2}\n\n"
        "{5}\n\n"
        "{6}\n\n"
        "Full per-event facts:\n\n{3}\n\n"
        "Write the situation summary now, using only the facts above."
    ).format(
        source, generated_at, len(analyses), facts, meta_lines,
        attention_block, scenario_block,
    )


# --------------------------------------------------------------------------- #
# llama.cpp client (stdlib only -- no httpx required)
# --------------------------------------------------------------------------- #

def check_llm_server(base_url: str, timeout: float = 5.0) -> bool:
    """GET /health on the llama.cpp server. True if it responds 200 OK."""
    url = base_url.rstrip("/") + "/health"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def call_llm(
    prompt: str,
    system_prompt: str,
    base_url: str,
    timeout: float = 120.0,
    max_tokens: int = 700,
    temperature: float = 0.3,
) -> Optional[str]:
    """
    POST to llama.cpp's OpenAI-compatible /v1/chat/completions endpoint.
    Returns the generated text, or None if the call failed for any reason
    (server down, timeout, bad response, model not loaded, etc).
    """
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": "local-model",  # llama-server ignores this in single-model mode
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"].strip()
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        KeyError,
        IndexError,
        ValueError,
    ) as exc:
        print("[report_generator] llama.cpp call failed: {0}".format(exc))
        return None


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def generate_report(
    payload: TelemetryPayload,
    source: str,
    llm_base_url: str,
    timeout: float = 120.0,
    max_tokens: int = 700,
    temperature: float = 0.3,
) -> Dict[str, Any]:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    events = payload.log_entry.event_details
    analyses = [analyze_event(ev) for ev in events]

    log_meta = {
        "process": payload.log_entry.process,
        "timetag": payload.log_entry.timetag,
        "log_type": payload.log_entry.log_type,
    }

    scenario_matches = match_scenarios(analyses, events)
    attention = _attention_items(analyses)
    at_a_glance = _at_a_glance(analyses, len(scenario_matches))

    prompt = _build_user_prompt(
        source, generated_at, analyses, log_meta, attention, scenario_matches
    )
    narrative = call_llm(
        prompt,
        SYSTEM_PROMPT,
        llm_base_url,
        timeout=timeout,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    llm_used = narrative is not None
    if narrative is None:
        narrative = _fallback_narrative(analyses, attention, scenario_matches)

    detail_section = "\n\n".join(_format_event_facts(a) for a in analyses)
    attention_section = (
        "\n".join("- {0}".format(i) for i in attention)
        if attention
        else "None flagged -- nothing here needs a second look."
    )
    scenario_section = format_matched_scenarios(scenario_matches)
    catalog_section = format_scenario_catalog()

    narrative_source = (
        "AI-generated (Llama 3.2 3B via llama.cpp)"
        if llm_used
        else "auto-generated fallback (llama.cpp server unreachable)"
    )

    meta_header = ""
    if log_meta.get("process"):
        meta_header += "Process          : {0}\n".format(log_meta["process"])
    if log_meta.get("timetag"):
        meta_header += "Log timestamp    : {0}\n".format(log_meta["timetag"])
    if log_meta.get("log_type"):
        meta_header += "Log type         : {0}\n".format(log_meta["log_type"])

    report_text = (
        "TELEMETRY REPORT\n"
        "=================\n"
        "Source           : {source}\n"
        "{meta_header}"
        "Generated at     : {generated_at}\n"
        "Narrative source : {narrative_source}\n"
        "\n"
        "AT A GLANCE\n"
        "-----------\n"
        "{at_a_glance}\n"
        "\n"
        "SITUATION SUMMARY\n"
        "-----------------\n"
        "{narrative}\n"
        "\n"
        "ATTENTION ITEMS\n"
        "---------------\n"
        "{attention_section}\n"
        "\n"
        "MATCHED SCENARIOS\n"
        "------------------\n"
        "{scenario_section}\n"
        "\n"
        "EVENT DETAILS (raw facts, for verification)\n"
        "---------------------------------------------\n"
        "{detail_section}\n"
        "\n"
        "REFERENCE: SCENARIO CATALOG (10 common spacecraft ops scenarios)\n"
        "-------------------------------------------------------------------\n"
        "{catalog_section}\n"
    ).format(
        source=source,
        meta_header=meta_header,
        generated_at=generated_at,
        narrative_source=narrative_source,
        at_a_glance=at_a_glance,
        narrative=narrative,
        attention_section=attention_section,
        scenario_section=scenario_section,
        detail_section=detail_section,
        catalog_section=catalog_section,
    )

    return {
        "report": report_text,
        "source": source,
        "generated_at": generated_at,
        "events_analyzed": len(analyses),
        "attention_items": attention,
        "matched_scenarios": [m["name"] for m in scenario_matches],
        "llm_used": llm_used,
    }
