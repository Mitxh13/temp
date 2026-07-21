"""
models.py
---------
Pydantic schema for the telemetry / event-log JSON.

The source JSON looks like:

{
  "logEntry": {
    "EventDetails": [
      {
        "Action": "Event1test.tst",
        "Enabled": true,
        "EventCondition": "PID(\"PWR05013\")<70",
        ...
      },
      { ... event 2 ... }
    ]
  }
}

Notes on field names:
- The raw JSON uses PascalCase for some keys, camelCase for others, and one
  key literally contains "(ms)" (RetriggeringTime(ms)) which is not a legal
  Python identifier. We map every raw key to a clean, readable Python name
  using Field(alias=...), and turn on `populate_by_name` so the models can
  be built either from raw JSON (aliases) or from Python kwargs (field names).
- `extra="allow"` is used everywhere because a couple of fields in the
  source photo were only partially visible (event 2 gets cut off). Any
  field we didn't explicitly model is kept instead of being dropped or
  raising a validation error.
"""

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class EventDetail(BaseModel):
    """One entry from logEntry.EventDetails[]."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    action: str = Field(alias="Action")
    enabled: bool = Field(alias="Enabled")
    event_condition: str = Field(alias="EventCondition")
    event_description: str = Field(alias="EventDescription")
    event_id: str = Field(alias="EventId")
    event_name: str = Field(alias="EventName")
    guard_condition: str = Field(alias="GuardCondition")
    retriggering_time_ms: int = Field(alias="RetriggeringTime(ms)")
    action_sts: str = Field(alias="actionSts")
    cur_no_of_samples_available: int = Field(alias="curNoOfSamplesAvailable")
    currently_monitored: bool = Field(alias="currentlyMonitored")
    evaluation_restart_countdown_ms: int = Field(
        alias="evaluationRestartCountDownInMs"
    )
    # These two hold the actual PID values sampled while evaluating the
    # condition / guard. They were empty in the example image but can in
    # principle contain numbers, strings or small objects, so we keep them
    # loosely typed on purpose.
    event_condition_pid_values: List[Any] = Field(
        default_factory=list, alias="eventConditionPidValues"
    )
    event_condition_sts: List[bool] = Field(
        default_factory=list, alias="eventConditionSts"
    )
    event_monitoring_health: str = Field(alias="eventMonitoringHealth")
    guard_condition_pid_values: List[Any] = Field(
        default_factory=list, alias="guardConditionPidValues"
    )
    guard_condition_sts: List[bool] = Field(
        default_factory=list, alias="guardConditionSts"
    )
    no_of_samples: int = Field(alias="noOfSamples")
    valid_event: bool = Field(alias="validEvent")


class LogEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    event_details: List[EventDetail] = Field(alias="EventDetails")
    # Revealed by the full log capture (previously the photo cut off before
    # these) -- metadata about the snapshot itself, sitting alongside
    # EventDetails rather than inside each event.
    log_type: Optional[str] = Field(default=None, alias="logType")
    process: Optional[str] = Field(default=None, alias="process")
    timetag: Optional[str] = Field(default=None, alias="timetag")


class TelemetryPayload(BaseModel):
    """Root object of the incoming JSON."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    log_entry: LogEntry = Field(alias="logEntry")
