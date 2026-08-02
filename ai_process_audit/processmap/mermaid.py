"""Render an inferred process map as Mermaid flowchart text.

Deterministic. No model is consulted here.

Mermaid is written as text and never rendered to an image by this engine. That keeps
the dependency list short and means the map can be pasted into any tool that already
speaks Mermaid. The report embeds the text in a fenced block.
"""

from __future__ import annotations

import re

from .steps import ProcessMap, Step

# Node shapes by step kind. Every label is quoted, so brackets and parentheses inside
# the text cannot break the diagram.
SHAPES: dict[str, tuple[str, str]] = {
    "task": ("[", "]"),
    "data_entry": ("[", "]"),
    "approval": ("{{", "}}"),
    "decision": ("{", "}"),
    "communication": ("(", ")"),
    "system": ("[[", "]]"),
    "wait": ("((", "))"),
}

LABEL_LIMIT = 72


def _escape(text: str) -> str:
    """Make a label safe to sit inside a quoted Mermaid node."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = cleaned.replace('"', "'")
    # Mermaid treats these as markup inside labels even when quoted.
    cleaned = cleaned.replace("#", "no. ").replace("<", "(").replace(">", ")")
    if len(cleaned) > LABEL_LIMIT:
        cleaned = cleaned[: LABEL_LIMIT - 3].rstrip() + "..."
    return cleaned


def _node(step: Step) -> str:
    open_shape, close_shape = SHAPES.get(step.kind, SHAPES["task"])
    label = _escape(step.text)
    if step.actor:
        label = f"{_escape(step.actor)}: {label}"
        if len(label) > LABEL_LIMIT:
            label = label[: LABEL_LIMIT - 3].rstrip() + "..."
    return f'{step.id}{open_shape}"{label}"{close_shape}'


def render_mermaid(process_map: ProcessMap, direction: str = "TD") -> str:
    """Render a process map as Mermaid flowchart source."""
    lines = [f"flowchart {direction}"]
    lines.append('    start(["Start"])')

    for step in process_map.steps:
        lines.append(f"    {_node(step)}")

    lines.append('    finish(["End"])')

    chain = ["start"] + [step.id for step in process_map.steps] + ["finish"]
    for before, after in zip(chain, chain[1:]):
        lines.append(f"    {before} --> {after}")

    # Style manual steps so the reader can see the automation surface at a glance.
    manual = [step.id for step in process_map.steps if step.kind in {"data_entry", "communication"}]
    waits = [step.id for step in process_map.steps if step.kind == "wait"]
    if manual:
        lines.append("    classDef manual fill:#fdf2e2,stroke:#b3762a,color:#3d2a0c;")
        lines.append(f"    class {','.join(manual)} manual;")
    if waits:
        lines.append("    classDef waiting fill:#eef1f5,stroke:#6b7785,color:#26303b;")
        lines.append(f"    class {','.join(waits)} waiting;")

    return "\n".join(lines)
