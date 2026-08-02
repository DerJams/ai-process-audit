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
class Rubric:
    """The full rubric, as read from rubric.md."""

    version: str
    approved: bool
    scale_min: int
    scale_max: int
    criteria: tuple[Criterion, ...]
    bands: tuple[Band, ...]
    source_path: Path

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
        for band in self.bands:
            if weighted_score >= band.min_score:
                return band
        return self.bands[-1]


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

    scale = spec["scale"]
    return Rubric(
        version=spec["version"],
        approved=bool(spec.get("approved", False)),
        scale_min=int(scale["min"]),
        scale_max=int(scale["max"]),
        criteria=tuple(criteria),
        bands=bands,
        source_path=path,
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
