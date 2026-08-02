"""Turn a validated intake dictionary into the internal process model.

Deterministic. No model is consulted here.

The only judgement this module makes is converting stated frequencies and volumes
into yearly figures. Those conversion factors are constants below rather than hidden
in the code, because they are assumptions and a reader should be able to argue with
them.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .models import Business, Intake, People, Process, TimeSpent, Volume

# A working year, used to turn a weekly time figure into a yearly one. Deliberately
# below 52 so that holiday and closure weeks are not counted as working ones.
WORKING_WEEKS_PER_YEAR = 48.0

# How many times a year each stated frequency runs. A working year is treated as
# 250 days and 52 weeks. Continuous is read as roughly hourly through a working day.
RUNS_PER_YEAR: dict[str, float] = {
    "continuous": 2000.0,
    "daily": 250.0,
    "several_times_per_week": 150.0,
    "weekly": 52.0,
    "monthly": 12.0,
    "quarterly": 4.0,
    "annually": 1.0,
    "ad_hoc": 12.0,
}

# ad_hoc has no defined rate, so the engine assumes monthly and flags the assumption
# so that it can be shown in the report rather than buried.
ASSUMED_FREQUENCIES = frozenset({"ad_hoc"})

# How many of each period fall in a year.
PERIODS_PER_YEAR: dict[str, float] = {
    "per_day": 250.0,
    "per_week": 52.0,
    "per_month": 12.0,
    "per_quarter": 4.0,
    "per_year": 1.0,
}


def slugify(text: str, fallback: str = "process") -> str:
    """Make a stable, readable identifier from a name.

    Used only when an intake omits an explicit id. Gold labels key on the id, so an
    intake that will be labelled should set ids explicitly rather than rely on this.
    """
    normalised = unicodedata.normalize("NFKD", text)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug or not slug[0].isalnum():
        slug = fallback
    return slug[:64]


def items_per_year(count: float, period: str, runs_per_year: float) -> float:
    """Convert a stated volume into items a year."""
    if period == "per_run":
        return count * runs_per_year
    return count * PERIODS_PER_YEAR[period]


def _normalize_volume(raw: dict[str, Any], runs_per_year: float) -> Volume:
    count = float(raw["count"])
    period = raw["period"]
    return Volume(
        count=count,
        unit=raw["unit"].strip(),
        period=period,
        items_per_year=items_per_year(count, period, runs_per_year),
        is_estimate=bool(raw.get("is_estimate", False)),
    )


def _normalize_time_spent(raw: dict[str, Any], items_a_year: float) -> TimeSpent:
    """Turn a stated time figure into hours a year.

    The schema requires at least one of the two, so one branch always applies.
    """
    hours_per_week = raw.get("hours_per_week")
    minutes_per_case = raw.get("minutes_per_case")

    from_cases = None
    if minutes_per_case is not None:
        from_cases = float(minutes_per_case) * items_a_year / 60.0

    if hours_per_week is not None:
        hours_per_year = float(hours_per_week) * WORKING_WEEKS_PER_YEAR
        basis = (
            f"{float(hours_per_week):g} hours a week over "
            f"{WORKING_WEEKS_PER_YEAR:g} working weeks"
        )
    else:
        hours_per_year = from_cases or 0.0
        basis = (
            f"{float(minutes_per_case):g} minutes per item over "
            f"{int(items_a_year):,} items a year"
        )

    return TimeSpent(
        hours_per_week=float(hours_per_week) if hours_per_week is not None else None,
        minutes_per_case=float(minutes_per_case) if minutes_per_case is not None else None,
        hours_per_year=round(hours_per_year, 2),
        basis=basis,
        hours_per_year_from_cases=round(from_cases, 2) if from_cases is not None else None,
    )


def _normalize_people(raw: dict[str, Any], runs_per_year: float) -> People:
    hours_per_run = raw.get("hours_per_run")
    hours_per_year = None
    if hours_per_run is not None:
        hours_per_year = float(hours_per_run) * runs_per_year
    return People(
        count=int(raw["count"]),
        roles=tuple(role.strip() for role in raw.get("roles", [])),
        hours_per_run=float(hours_per_run) if hours_per_run is not None else None,
        hours_per_year=hours_per_year,
    )


def _normalize_process(raw: dict[str, Any], index: int, seen_ids: set[str]) -> Process:
    frequency = raw["frequency"]
    runs_per_year = RUNS_PER_YEAR[frequency]

    process_id = raw.get("id") or slugify(raw["name"], fallback=f"process-{index + 1}")
    if process_id in seen_ids:
        # Two processes with the same name would otherwise collide in gold labels,
        # which key on the id. Suffix rather than fail, and keep it predictable.
        process_id = f"{process_id}-{index + 1}"
    seen_ids.add(process_id)

    volume = _normalize_volume(raw["volume"], runs_per_year)

    return Process(
        id=process_id,
        name=raw["name"].strip(),
        description=raw["description"].strip(),
        pain_description=raw["pain_description"].strip(),
        frequency=frequency,
        runs_per_year=runs_per_year,
        frequency_is_assumed=frequency in ASSUMED_FREQUENCIES,
        volume=volume,
        people=_normalize_people(raw["people_involved"], runs_per_year),
        time_spent=_normalize_time_spent(raw["time_spent"], volume.items_per_year),
        current_tools=tuple(tool.strip() for tool in raw.get("current_tools", [])),
        risk_flags=tuple(sorted(raw.get("risk_flags", []))),
        data_notes=(raw.get("data_notes") or "").strip() or None,
        owner=(raw.get("owner") or "").strip() or None,
        decision_type=raw.get("decision_type"),
        baseline_metric=(raw.get("baseline_metric") or "").strip() or None,
        customer_facing=raw.get("customer_facing"),
    )


def normalize_intake(raw: dict[str, Any]) -> Intake:
    """Build the internal model from a validated intake document."""
    raw_business = raw["business"]
    business = Business(
        industry=raw_business["industry"].strip(),
        headcount=int(raw_business["headcount"]),
        tools_in_use=tuple(tool.strip() for tool in raw_business.get("tools_in_use", [])),
        name=(raw_business.get("name") or "").strip() or None,
        notes=(raw_business.get("notes") or "").strip() or None,
    )

    seen_ids: set[str] = set()
    processes = tuple(
        _normalize_process(raw_process, index, seen_ids)
        for index, raw_process in enumerate(raw["processes"])
    )

    intake_id = raw.get("intake_id") or slugify(business.name or business.industry, "intake")

    return Intake(
        intake_id=intake_id,
        business=business,
        processes=processes,
        collected_on=raw.get("collected_on"),
        schema_version=raw["schema_version"],
    )
