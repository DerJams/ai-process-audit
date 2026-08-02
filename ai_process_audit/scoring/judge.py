"""The judge: the single point in this system where a model may be consulted.

This is the only module permitted to call a model API. Every other module in the
package is deterministic. If you find yourself wanting to call a model from
anywhere else, that is a signal the pipeline boundary is in the wrong place, and the
right fix is to move the judgement here.

Current state: stub mode only. LiveJudge is defined so the interface is real and
testable, but it raises rather than making a call. No network code exists in this
file. Turning it on is a deliberate, reviewed change, not a configuration flag.

The judge answers exactly one question per criterion: a score on the rubric scale
and one sentence saying why. It does not rank, it does not weight, and it does not
decide the recommendation band. Those are deterministic and live in score.py.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol, runtime_checkable

from ..model.models import Process
from ..processmap.steps import ProcessMap
from .rubric import Rubric

# Set to "1" to permit live mode. Checked in addition to an explicit mode argument,
# so a live call needs both an environment change and a code path that asks for it.
LIVE_ENV_FLAG = "AI_PROCESS_AUDIT_ALLOW_LIVE"


@dataclass(frozen=True)
class CriterionScore:
    """A score and its one sentence justification."""

    criterion_id: str
    score: int
    rationale: str


@dataclass(frozen=True)
class JudgeVerdict:
    """Everything a judge returns about one process."""

    process_id: str
    judge_id: str
    mode: str
    rubric_version: str
    scores: dict[str, CriterionScore] = field(default_factory=dict)

    def score_for(self, criterion_id: str) -> CriterionScore:
        try:
            return self.scores[criterion_id]
        except KeyError:
            raise KeyError(
                f"Judge {self.judge_id} returned no score for criterion {criterion_id!r}"
            ) from None


@runtime_checkable
class Judge(Protocol):
    """What the pipeline requires of any judge, stub or live."""

    judge_id: str
    mode: str

    def judge(self, process: Process, process_map: ProcessMap, rubric: Rubric) -> JudgeVerdict:
        """Score one process against every criterion in the rubric."""
        ...


def _clamp(value: float, rubric: Rubric) -> int:
    return int(max(rubric.scale_min, min(rubric.scale_max, round(value))))


@lru_cache(maxsize=512)
def _word_pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(phrase.strip())}\b", re.IGNORECASE)


def _mentions(haystack: str, phrase: str) -> bool:
    """Whole word match.

    Substring matching looked fine until an intake listed Reapit as its system, which
    contains the letters api, and data availability jumped to the top of the scale on
    the strength of it. Keyword matching needs word boundaries or it invents evidence.
    """
    return _word_pattern(phrase).search(haystack) is not None


# Signals used by the stub. Each tier is a list of words, and the highest tier with a
# match wins. These are placeholders for a real judgement, and they are kept in the
# open here so that nobody mistakes them for one.
_DATA_TIERS: list[tuple[int, tuple[str, ...]]] = [
    (1, ("paper", "handwritten", "hand written", "verbal", "in their head", "whiteboard",
         "notebook", "post-it", "memory", "phone call")),
    (2, ("scanned", "scan", "photo", "photos", "pdf", "free text", "attachment", "fax",
         "screenshot", "voicemail")),
    (3, ("spreadsheet", "excel", "google sheet", "google sheets", "csv", "email", "inbox",
         "shared drive", "dropbox")),
    (4, ("system", "database", "crm", "accounting software", "portal", "software",
         "xero", "quickbooks", "sage", "salesforce", "hubspot", "shopify", "stripe",
         "square", "monday", "jobber", "servicetitan")),
    (5, ("api", "integration", "webhook", "export to csv", "reporting module",
         "data export", "sql")),
]

_PAIN_WORDS: tuple[str, ...] = (
    "error", "errors", "mistake", "mistakes", "wrong", "late", "missed", "miss",
    "chase", "chasing", "complaint", "complaints", "angry", "upset", "stress",
    "stressful", "overtime", "weekend", "evening", "quit", "left", "burnout",
    "fine", "penalty", "lost", "losing", "rework", "twice", "duplicate", "backlog",
    "argument", "escalate", "refund", "write off", "bad debt",
)

_RISK_BY_FLAG: dict[str, int] = {
    "safety_critical": 5,
    "legally_binding": 5,
    "regulated_data": 4,
    "handles_money": 4,
    "customer_facing": 3,
}


class StubJudge:
    """A deterministic placeholder judge.

    It reads the same normalised signals a person would look at first, and turns
    them into scores with fixed thresholds. It is not a model, it is not a
    judgement, and agreement numbers produced against it say nothing about how well
    a real judge would perform. Its purpose is to let the rest of the pipeline be
    built, tested, and reviewed without spending a token.

    Two behaviours are available. The heuristic behaviour varies with the intake,
    which is what the eval harness and the report templates need in order to be
    exercised properly. The fixed behaviour returns the midpoint for everything,
    which is useful for testing that aggregation and ranking do not depend on the
    judge.
    """

    def __init__(self, behaviour: str = "heuristic") -> None:
        if behaviour not in {"heuristic", "fixed"}:
            raise ValueError(
                f"Unknown stub behaviour {behaviour!r}. Use 'heuristic' or 'fixed'."
            )
        self.behaviour = behaviour
        self.judge_id = f"stub-{behaviour}-v1"
        self.mode = "stub"

    def judge(self, process: Process, process_map: ProcessMap, rubric: Rubric) -> JudgeVerdict:
        if self.behaviour == "fixed":
            midpoint = (rubric.scale_min + rubric.scale_max) // 2
            scores = {
                criterion.id: CriterionScore(
                    criterion.id,
                    midpoint,
                    "Fixed stub mode returns the midpoint for every criterion.",
                )
                for criterion in rubric.criteria
            }
            return JudgeVerdict(process.id, self.judge_id, self.mode, rubric.version, scores)

        handlers = {
            "pain": self._pain,
            "frequency": self._frequency,
            "volume": self._volume,
            "data_availability": self._data_availability,
            "implementation_risk": self._implementation_risk,
            "return_band": self._return_band,
        }

        scores: dict[str, CriterionScore] = {}
        for criterion in rubric.criteria:
            handler = handlers.get(criterion.id)
            if handler is None:
                # A criterion was added to rubric.md that the stub does not know how
                # to fake. Return the midpoint and say so plainly rather than
                # guessing, so the gap shows up in the report and the eval output.
                midpoint = (rubric.scale_min + rubric.scale_max) // 2
                scores[criterion.id] = CriterionScore(
                    criterion.id,
                    midpoint,
                    f"The stub judge has no rule for {criterion.label}, so it returned the midpoint.",
                )
                continue
            raw_score, rationale = handler(process, process_map)
            scores[criterion.id] = CriterionScore(
                criterion.id, _clamp(raw_score, rubric), rationale
            )

        return JudgeVerdict(process.id, self.judge_id, self.mode, rubric.version, scores)

    def _pain(self, process: Process, process_map: ProcessMap) -> tuple[int, str]:
        text = process.pain_description
        hits = sorted({word for word in _PAIN_WORDS if _mentions(text, word)})
        handoffs = process_map.handoff_count
        score = 1 + min(3, len(hits)) + (1 if handoffs >= 2 else 0)
        if not hits:
            return 2, (
                "The pain description does not name a specific error, delay, or cost, "
                "so this scores low on the evidence given."
            )
        shown = ", ".join(hits[:3])
        return score, (
            f"The pain description names {len(hits)} sign(s) of trouble including {shown}, "
            f"across a process with {handoffs} handoff(s) between people."
        )

    def _frequency(self, process: Process, process_map: ProcessMap) -> tuple[int, str]:
        runs = process.runs_per_year
        if runs >= 250:
            score = 5
        elif runs >= 52:
            score = 4
        elif runs >= 12:
            score = 3
        elif runs >= 4:
            score = 2
        else:
            score = 1
        assumed = (
            " The intake said ad hoc, so monthly was assumed."
            if process.frequency_is_assumed
            else ""
        )
        return score, (
            f"Stated frequency of {process.frequency.replace('_', ' ')} works out at about "
            f"{int(runs)} runs a year.{assumed}"
        )

    def _volume(self, process: Process, process_map: ProcessMap) -> tuple[int, str]:
        items = process.volume.items_per_year
        if items > 10000:
            score = 5
        elif items >= 1000:
            score = 4
        elif items >= 250:
            score = 3
        elif items >= 50:
            score = 2
        else:
            score = 1
        estimate = " The business gave this as an estimate." if process.volume.is_estimate else ""
        return score, (
            f"About {int(items):,} {process.volume.unit} a year, from {process.volume.describe()}."
            f"{estimate}"
        )

    def _data_availability(self, process: Process, process_map: ProcessMap) -> tuple[int, str]:
        haystack = " ".join([process.all_text, " ".join(process.current_tools)])
        matched: list[tuple[int, str]] = []
        for tier, words in _DATA_TIERS:
            for word in words:
                if _mentions(haystack, word):
                    matched.append((tier, word))
                    break
        if not matched:
            return 3, (
                "The intake does not say where the information for this process lives, "
                "so it scores at the midpoint by default."
            )
        best_tier, best_word = max(matched, key=lambda pair: pair[0])
        worst_tier, worst_word = min(matched, key=lambda pair: pair[0])
        if best_tier == worst_tier:
            return best_tier, (
                f"The intake points at {best_word} as where this information lives."
            )
        return best_tier, (
            f"The best source named is {best_word}, though {worst_word} also appears, "
            "so part of the input may need manual handling."
        )

    def _implementation_risk(self, process: Process, process_map: ProcessMap) -> tuple[int, str]:
        if process.risk_flags:
            score = max(_RISK_BY_FLAG.get(flag, 3) for flag in process.risk_flags)
            named = ", ".join(flag.replace("_", " ") for flag in process.risk_flags)
            return score, (
                f"The intake flags this process as {named}, which raises the cost of a "
                "wrong output."
            )
        approvals = sum(1 for step in process_map.steps if step.kind == "approval")
        external = any(step.actor_is_external for step in process_map.steps)
        score = 2 + (1 if external else 0) - (1 if approvals >= 2 else 0)
        return max(1, score), (
            f"No risk flags were reported, the map shows {approvals} checking step(s), and the "
            f"process does{'' if external else ' not'} reach people outside the business."
        )

    def _return_band(self, process: Process, process_map: ProcessMap) -> tuple[int, str]:
        hours = process.people.hours_per_year
        if hours is None:
            return 3, (
                "The intake does not say how long this process takes, so the return cannot "
                "be estimated and scores at the midpoint."
            )
        if hours >= 250:
            score = 5
        elif hours >= 100:
            score = 4
        elif hours >= 24:
            score = 3
        elif hours >= 6:
            score = 2
        else:
            score = 1
        per_run = process.people.hours_per_run or 0
        per_run_text = f"{per_run:g}"
        return score, (
            f"About {int(hours):,} person hours a year go into this process, from "
            f"{per_run_text} hours per run across {process.people.count} people."
        )


class LiveJudge:
    """Placeholder for the model backed judge. Deliberately not implemented.

    When this is built, it belongs in this class and nowhere else. The contract it
    must meet:

    1. One call per process, not one per criterion, so that the judge sees the whole
       process when scoring any part of it.
    2. Return a score and a one sentence rationale for every criterion in the rubric
       that is passed in, and nothing else. No ranking, no weighting, no band.
    3. Refuse to invent a score when the intake is silent. The rubric says score 3
       and say so, and the live judge must follow that instruction.
    4. Record the rubric version it scored against in the verdict, so that a stored
       result can never be compared against labels from a different rubric.
    5. Be reproducible enough to evaluate. Pin the model version and record it in
       judge_id.
    """

    def __init__(self, model: str = "unset") -> None:
        self.model = model
        self.judge_id = f"live-{model}"
        self.mode = "live"

    def judge(self, process: Process, process_map: ProcessMap, rubric: Rubric) -> JudgeVerdict:
        raise NotImplementedError(
            "Live judging is not implemented. This project runs in stub mode by design, "
            "and adding a real API call is a reviewed change rather than a setting."
        )


def get_judge(mode: str = "stub", behaviour: str = "heuristic") -> Judge:
    """Return a judge.

    Stub is the default and is the only mode that works today. Asking for live mode
    fails loudly, and fails even earlier when the environment has not opted in, so
    that no accidental call can happen.
    """
    if mode == "stub":
        return StubJudge(behaviour=behaviour)
    if mode == "live":
        if os.environ.get(LIVE_ENV_FLAG) != "1":
            raise RuntimeError(
                "Live judging was requested but is not permitted. Set "
                f"{LIVE_ENV_FLAG}=1 to opt in. Note that live mode is also not "
                "implemented yet, so this will still fail after opting in."
            )
        return LiveJudge()
    raise ValueError(f"Unknown judge mode {mode!r}. Use 'stub' or 'live'.")
