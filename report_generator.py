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

SYSTEM_PROMPT = (
    "You are a telemetry analyst. You write short, clear, plain-English reports "
    "for engineers, based ONLY on the facts given to you below. "
    "Never invent numbers, PID tags, or statuses that are not in the facts. "
    "Do not use markdown headings, bullet points, or code fences -- write in "
    "plain paragraphs. Structure your answer as one short paragraph per event "
    "(refer to each event by its EventId and EventName), followed by one short "
    "overall-summary paragraph at the end."
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


def _logic_note(condition_state: str, guard_state: str) -> str:
    if condition_state == "TRUE" and guard_state == "TRUE":
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
    health_ok = ev.event_monitoring_health.strip().upper() == "MON OK"
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
        "logic_note": _logic_note(condition_state, guard_state),
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


def _fallback_narrative(analyses: List[Dict[str, Any]]) -> str:
    """Used only if the llama.cpp server can't be reached."""
    lines = []
    for a in analyses:
        lines.append(
            'Event {0} ("{1}"): {2}. Action status is currently {3}.'.format(
                a["event_id"], a["event_name"], a["logic_note"], a["action_sts"]
            )
        )
    lines.append(
        "(This summary was generated without the language model, because the "
        "llama.cpp server could not be reached. Only the deterministic facts "
        "below were used.)"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Prompt building
# --------------------------------------------------------------------------- #

def _build_user_prompt(
    source: str, generated_at: str, analyses: List[Dict[str, Any]]
) -> str:
    facts = "\n\n".join(_format_event_facts(a) for a in analyses)
    return (
        "Source: {0}\n"
        "Report generated at: {1}\n"
        "Number of events: {2}\n\n"
        "Facts extracted from the telemetry log:\n\n{3}\n\n"
        "Write the report now, using only the facts above."
    ).format(source, generated_at, len(analyses), facts)


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
    analyses = [analyze_event(ev) for ev in payload.log_entry.event_details]

    prompt = _build_user_prompt(source, generated_at, analyses)
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
        narrative = _fallback_narrative(analyses)

    detail_section = "\n\n".join(_format_event_facts(a) for a in analyses)

    narrative_source = (
        "AI-generated (Llama 3.2 3B via llama.cpp)"
        if llm_used
        else "auto-generated fallback (llama.cpp server unreachable)"
    )

    report_text = (
        "TELEMETRY REPORT\n"
        "=================\n"
        "Source           : {source}\n"
        "Generated at     : {generated_at}\n"
        "Events analyzed  : {count}\n"
        "Narrative source : {narrative_source}\n"
        "\n"
        "SUMMARY\n"
        "-------\n"
        "{narrative}\n"
        "\n"
        "EVENT DETAILS (raw facts, for verification)\n"
        "---------------------------------------------\n"
        "{detail_section}\n"
    ).format(
        source=source,
        generated_at=generated_at,
        count=len(analyses),
        narrative_source=narrative_source,
        narrative=narrative,
        detail_section=detail_section,
    )

    return {
        "report": report_text,
        "source": source,
        "generated_at": generated_at,
        "events_analyzed": len(analyses),
        "llm_used": llm_used,
    }
