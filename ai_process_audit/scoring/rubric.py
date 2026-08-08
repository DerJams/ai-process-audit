"""Load the rubric from rubric.md.

Deterministic. No model is consulted here.

The rubric lives in a markdown file so that the definitions a human argues about and
the definitions the code applies cannot drift apart. Code reads one fenced block
inside that file, marked rubric-spec. Everything else in rubric.md is for people.

If the block and the prose disagree, that is a bug in the rubric, not in this module.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_RUBRIC_PATH = Path(__file__).resolve().parents[2] / "rubric.md"

_SPEC_BLOCK = re.compile(r"```rubric-spec\s*\n(.*?)\n```", re.DOTALL)

VALID_DIRECTIONS = frozenset({"higher_is_better", "higher_is_worse"})

# The conditions a criterion cap may test. Each one is evaluated against a process in
# scoring/score.py. Keeping the list closed means a rubric cannot ask for something
# the engine does not know how to check, and a typo fails at load rather than
# silently never firing.
CAP_CONDITIONS = frozenset({"no_baseline_metric"})

# Weights are floats, so an exact sum to 1.0 is not guaranteed by arithmetic.
WEIGHT_TOLERANCE = 1e-6


class RubricError(Exception):
    """Raised when rubric.md is missing, malformed, or internally inconsistent."""


@dataclass(frozen=True)
class Criterion:
    """One scoring criterion."""

    id: str
    label: str
    weight: float
    direction: str
    question: str

    @property
    def is_inverted(self) -> bool:
        return self.direction == "higher_is_worse"


@dataclass(frozen=True)
class Band:
    """A recommendation band and the weighted score at which it starts."""

    id: str
    label: str
    min_score: float


@dataclass(frozen=True)
class BandCap:
    """A rule that limits the recommendation regardless of the weighted score.

    A weighted average lets five good criteria outvote one bad one, which is correct
    for a score and wrong for a recommendation when the bad one is that a mistake
    would be a serious event. A cap sits outside the arithmetic and never changes the
    score, only what may be recommended on the strength of it.
    """

    criterion: str
    at_or_above: int
    max_band: str
    reason: str


@dataclass(frozen=True)
class CriterionCap:
    """A rule that limits one criterion score before the weighting is applied.

    Unlike a band cap, this does change the weighted score, because it changes an
    input to it. It exists for the case where a criterion cannot honestly be scored
    high on the evidence available, whatever the underlying facts might be.

    The condition is one of a small fixed vocabulary evaluated in scoring, listed in
    CAP_CONDITIONS. It is deliberately not a general expression language.
    """

    criterion: str
    condition: str
    max_score: int
    reason: str


@dataclass(frozen=True)
class AppliedCriterionCap:
    """A record that a criterion cap lowered a score, kept so the report can say so."""

    cap: CriterionCap
    criterion_label: str
    score_before: int
    score_after: int


@dataclass(frozen=True)
class Disqualification:
    """A rule that removes a process from the ranking instead of scoring it.

    The third of the three mechanisms that sit outside the weighted average. A cap
    adjusts a number. This produces no number at all, because a low score still
    invites comparison and comparing a process software cannot perform against ones it
    can is the comparison itself being wrong.
    """

    criterion: str
    at_or_below: int
    reason: str
    referral: str


@dataclass(frozen=True)
class AppliedCap:
    """A record that a cap changed the recommendation, kept so the report can say so."""

    cap: BandCap
    criterion_label: str
    raw_score: int
    band_before: Band
    band_after: Band


@dataclass(frozen=True)
class Rubric:
    """The full rubric, as read from rubric.md."""

    version: str
    approved: bool
    scale_min: int
    scale_max: int
    criteria: tuple[Criterion, ...]
    bands: tuple[Band, ...]
    source_path: Path
    band_caps: tuple[BandCap, ...] = ()
    criterion_caps: tuple[CriterionCap, ...] = ()
    disqualifications: tuple[Disqualification, ...] = ()

    @property
    def criterion_ids(self) -> tuple[str, ...]:
        return tuple(criterion.id for criterion in self.criteria)

    def criterion(self, criterion_id: str) -> Criterion:
        for candidate in self.criteria:
            if candidate.id == criterion_id:
                return candidate
        raise RubricError(f"No criterion with id {criterion_id!r} in rubric {self.version}")

    def effective_score(self, criterion_id: str, raw_score: float) -> float:
        """Apply direction so that higher always means a better candidate.

        Inversion happens here and nowhere else. A judge always scores a criterion in
        its own natural direction, so implementation risk is scored as risk.
        """
        criterion = self.criterion(criterion_id)
        if criterion.is_inverted:
            return float(self.scale_min + self.scale_max - raw_score)
        return float(raw_score)

    def band_for(self, weighted_score: float) -> Band:
        """The band a score earns before any cap is considered."""
        for band in self.bands:
            if weighted_score >= band.min_score:
                return band
        return self.bands[-1]

    def disqualification_for(self, scores: dict[str, int]) -> Disqualification | None:
        """The first disqualification these scores trigger, if any."""
        for rule in self.disqualifications:
            score = scores.get(rule.criterion)
            if score is not None and score <= rule.at_or_below:
                return rule
        return None

    def band_by_id(self, band_id: str) -> Band:
        for band in self.bands:
            if band.id == band_id:
                return band
        raise RubricError(f"No band with id {band_id!r} in rubric {self.version}")

    def apply_caps(
        self, band: Band, raw_scores: dict[str, int]
    ) -> tuple[Band, tuple[AppliedCap, ...]]:
        """Lower the band where a cap rule applies.

        Returns the band to recommend and a record of every cap that bit. The
        weighted score is never touched, so a reader can always see the number the
        cap overrode. Where several caps apply, the lowest ceiling wins.
        """
        applied: list[AppliedCap] = []
        current = band
        for cap in self.band_caps:
            raw = raw_scores.get(cap.criterion)
            if raw is None or raw < cap.at_or_above:
                continue
            ceiling = self.band_by_id(cap.max_band)
            if current.min_score <= ceiling.min_score:
                continue  # already at or below the ceiling, so the cap changes nothing
            applied.append(
                AppliedCap(
                    cap=cap,
                    criterion_label=self.criterion(cap.criterion).label,
                    raw_score=raw,
                    band_before=current,
                    band_after=ceiling,
                )
            )
            current = ceiling
        return current, tuple(applied)


def _parse_spec(text: str, path: Path) -> dict:
    match = _SPEC_BLOCK.search(text)
    if match is None:
        raise RubricError(
            f"{path} has no ```rubric-spec block. The engine reads the rubric from "
            "that block, so it cannot score without it."
        )
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RubricError(f"The rubric-spec block in {path} is not valid JSON: {exc.msg}") from None


def _build_rubric(spec: dict, path: Path) -> Rubric:
    required = {"version", "scale", "criteria", "bands"}
    missing = sorted(required - spec.keys())
    if missing:
        raise RubricError(f"Rubric spec in {path} is missing: {', '.join(missing)}")

    criteria = []
    seen_ids: set[str] = set()
    for entry in spec["criteria"]:
        if entry["id"] in seen_ids:
            raise RubricError(f"Rubric spec in {path} defines criterion {entry['id']!r} twice")
        seen_ids.add(entry["id"])
        if entry["direction"] not in VALID_DIRECTIONS:
            raise RubricError(
                f"Criterion {entry['id']!r} has direction {entry['direction']!r}, "
                f"which is not one of {sorted(VALID_DIRECTIONS)}"
            )
        criteria.append(
            Criterion(
                id=entry["id"],
                label=entry["label"],
                weight=float(entry["weight"]),
                direction=entry["direction"],
                question=entry.get("question", ""),
            )
        )

    if not criteria:
        raise RubricError(f"Rubric spec in {path} defines no criteria")

    total_weight = sum(criterion.weight for criterion in criteria)
    if abs(total_weight - 1.0) > WEIGHT_TOLERANCE:
        raise RubricError(
            f"Criterion weights in {path} sum to {total_weight:.6f}, not 1.0. "
            "Fix the weights in the rubric-spec block."
        )

    bands = tuple(
        sorted(
            (
                Band(id=entry["id"], label=entry["label"], min_score=float(entry["min_score"]))
                for entry in spec["bands"]
            ),
            key=lambda band: band.min_score,
            reverse=True,
        )
    )
    if not bands:
        raise RubricError(f"Rubric spec in {path} defines no bands")

    criterion_caps: list[CriterionCap] = []
    for entry in spec.get("criterion_caps", []):
        if entry["criterion"] not in seen_ids:
            raise RubricError(
                f"Criterion cap in {path} refers to criterion {entry['criterion']!r}, "
                "which is not defined in this rubric"
            )
        if entry["condition"] not in CAP_CONDITIONS:
            raise RubricError(
                f"Criterion cap in {path} uses condition {entry['condition']!r}, which "
                f"the engine cannot evaluate. Known conditions: {sorted(CAP_CONDITIONS)}"
            )
        criterion_caps.append(
            CriterionCap(
                criterion=entry["criterion"],
                condition=entry["condition"],
                max_score=int(entry["max_score"]),
                reason=entry.get("reason", ""),
            )
        )

    band_ids = {band.id for band in bands}
    caps: list[BandCap] = []
    for entry in spec.get("band_caps", []):
        if entry["criterion"] not in seen_ids:
            raise RubricError(
                f"Band cap in {path} refers to criterion {entry['criterion']!r}, "
                "which is not defined in this rubric"
            )
        if entry["max_band"] not in band_ids:
            raise RubricError(
                f"Band cap in {path} refers to band {entry['max_band']!r}, "
                "which is not defined in this rubric"
            )
        caps.append(
            BandCap(
                criterion=entry["criterion"],
                at_or_above=int(entry["at_or_above"]),
                max_band=entry["max_band"],
                reason=entry.get("reason", ""),
            )
        )

    disqualifications: list[Disqualification] = []
    for entry in spec.get("disqualifications", []):
        if entry["criterion"] not in seen_ids:
            raise RubricError(
                f"Disqualification in {path} refers to criterion "
                f"{entry['criterion']!r}, which is not defined in this rubric"
            )
        if not entry.get("referral"):
            raise RubricError(
                f"Disqualification on {entry['criterion']!r} in {path} has no referral. "
                "A process removed from the ranking must be pointed somewhere useful, "
                "because silence reads as a verdict on the business."
            )
        disqualifications.append(
            Disqualification(
                criterion=entry["criterion"],
                at_or_below=int(entry["at_or_below"]),
                reason=entry.get("reason", ""),
                referral=entry["referral"],
            )
        )

    scale = spec["scale"]
    return Rubric(
        version=spec["version"],
        approved=bool(spec.get("approved", False)),
        scale_min=int(scale["min"]),
        scale_max=int(scale["max"]),
        criteria=tuple(criteria),
        bands=bands,
        source_path=path,
        band_caps=tuple(caps),
        criterion_caps=tuple(criterion_caps),
        disqualifications=tuple(disqualifications),
    )


@lru_cache(maxsize=8)
def _load_cached(path_str: str, mtime: float) -> Rubric:
    path = Path(path_str)
    return _build_rubric(_parse_spec(path.read_text(encoding="utf-8"), path), path)


def load_rubric(path: str | Path | None = None) -> Rubric:
    """Read and validate the rubric.

    Results are cached on the file modification time, so editing rubric.md during a
    session is picked up without restarting.
    """
    path = Path(path) if path is not None else DEFAULT_RUBRIC_PATH
    if not path.exists():
        raise RubricError(f"Rubric file not found: {path}")
    return _load_cached(str(path), path.stat().st_mtime)
