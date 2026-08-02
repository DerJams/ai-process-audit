"""Validate an intake file against the JSON Schema.

Deterministic. No model is consulted here.

The validator reports every error in one pass rather than stopping at the first,
because an intake is usually filled in by a person who would rather fix ten things
at once than run the tool ten times.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).with_name("schema.json")


@dataclass(frozen=True)
class ValidationIssue:
    """One problem with an intake file."""

    location: str
    message: str

    def __str__(self) -> str:
        return f"{self.location}: {self.message}"


class IntakeValidationError(Exception):
    """Raised when an intake file does not satisfy the schema."""

    def __init__(self, path: Path | None, issues: list[ValidationIssue]) -> None:
        self.path = path
        self.issues = issues
        where = f" in {path}" if path is not None else ""
        detail = "\n".join(f"  {issue}" for issue in issues)
        super().__init__(
            f"Found {len(issues)} problem(s){where}:\n{detail}"
        )


def _load_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _format_location(path_parts: Any) -> str:
    parts = list(path_parts)
    if not parts:
        return "(document root)"
    rendered = ""
    for part in parts:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered += f".{part}" if rendered else str(part)
    return rendered


def validate_intake(data: Any, path: Path | None = None) -> dict[str, Any]:
    """Validate a parsed intake document.

    Returns the document unchanged when it is valid. Raises IntakeValidationError
    listing every problem when it is not.
    """
    validator = Draft202012Validator(_load_schema())
    errors = sorted(validator.iter_errors(data), key=lambda err: list(err.absolute_path))
    if errors:
        issues = [
            ValidationIssue(_format_location(error.absolute_path), error.message)
            for error in errors
        ]
        raise IntakeValidationError(path, issues)
    return data


def load_intake(path: str | Path) -> dict[str, Any]:
    """Read and validate an intake file from disk."""
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise IntakeValidationError(
            path, [ValidationIssue("(file)", "file does not exist")]
        ) from None
    except json.JSONDecodeError as exc:
        raise IntakeValidationError(
            path, [ValidationIssue(f"(line {exc.lineno})", f"file is not valid JSON: {exc.msg}")]
        ) from None
    return validate_intake(data, path)
