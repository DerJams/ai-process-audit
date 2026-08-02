"""The internal process model.

Everything downstream of normalisation reads these objects and never the raw intake
dictionary. That keeps the raw intake format free to change without touching the
process map, the scorer, or the report.

All objects are frozen. Nothing in the pipeline mutates the model after it is built.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Volume:
    """Throughput of a process, normalised to a yearly figure."""

    count: float
    unit: str
    period: str
    items_per_year: float
    is_estimate: bool = False

    def describe(self) -> str:
        readable = {
            "per_run": "each run",
            "per_day": "a day",
            "per_week": "a week",
            "per_month": "a month",
            "per_quarter": "a quarter",
            "per_year": "a year",
        }[self.period]
        count = int(self.count) if float(self.count).is_integer() else self.count
        return f"{count} {self.unit} {readable}"


@dataclass(frozen=True)
class TimeSpent:
    """How much time a process consumes, normalised to hours a year.

    This is the denominator behind the return band criterion. A business may answer
    in weekly hours, in minutes per item, or both. When both are given the stated
    weekly figure is treated as the primary one, because it is what the business
    actually observes, and the figure derived from minutes per item is kept beside it
    as a cross check rather than averaged into it.
    """

    hours_per_week: float | None
    minutes_per_case: float | None
    hours_per_year: float
    basis: str
    hours_per_year_from_cases: float | None = None

    @property
    def has_cross_check(self) -> bool:
        return (
            self.hours_per_week is not None
            and self.hours_per_year_from_cases is not None
        )

    def describe(self) -> str:
        if self.hours_per_week is not None:
            weekly = f"{self.hours_per_week:g}"
            return f"{weekly} hours a week"
        minutes = f"{self.minutes_per_case:g}"
        return f"{minutes} minutes per item"


@dataclass(frozen=True)
class People:
    """Who runs a process and how much of their time it takes."""

    count: int
    roles: tuple[str, ...] = ()
    hours_per_run: float | None = None
    hours_per_year: float | None = None


@dataclass(frozen=True)
class Process:
    """One business process, normalised."""

    id: str
    name: str
    description: str
    pain_description: str
    frequency: str
    runs_per_year: float
    frequency_is_assumed: bool
    volume: Volume
    people: People
    time_spent: TimeSpent
    current_tools: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    data_notes: str | None = None
    owner: str | None = None
    decision_type: str | None = None
    baseline_metric: str | None = None
    customer_facing: bool | None = None

    @property
    def has_baseline(self) -> bool:
        """Whether the business tracks any number for this process today.

        Absent and null mean the same thing here: nothing is tracked, so there would
        be no way to show an automation helped.
        """
        return bool(self.baseline_metric and self.baseline_metric.strip())

    @property
    def all_text(self) -> str:
        """Every free text field joined, for keyword scanning."""
        parts = [
            self.description,
            self.pain_description,
            self.data_notes or "",
            self.baseline_metric or "",
        ]
        return " ".join(part for part in parts if part)


@dataclass(frozen=True)
class Business:
    """The business the processes belong to."""

    industry: str
    headcount: int
    tools_in_use: tuple[str, ...] = ()
    name: str | None = None
    notes: str | None = None

    @property
    def display_name(self) -> str:
        return self.name or "This business"


@dataclass(frozen=True)
class Intake:
    """A validated and normalised intake."""

    intake_id: str
    business: Business
    processes: tuple[Process, ...] = field(default_factory=tuple)
    collected_on: str | None = None
    schema_version: str = "1.1.0"
