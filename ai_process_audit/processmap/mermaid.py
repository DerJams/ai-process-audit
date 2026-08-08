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

# What each shape means, for the legend. Only the kinds actually used are drawn.
KIND_MEANINGS: dict[str, str] = {
    "task": "Step done by hand",
    "data_entry": "Typing or copying data",
    "approval": "Check or approval",
    "decision": "Decision point",
    "communication": "Message to someone",
    "system": "Done in a system",
    "wait": "Waiting on someone",
}


def _shorten(text: str, limit: int) -> str:
    """Cut a label to length on a word boundary.

    Cutting mid word produced labels like "custome..." which read as a rendering fault
    rather than an abbreviation, and made the map look careless to anyone reading it.
    """
    if len(text) <= limit:
        return text
    cut = text[: limit - 3].rsplit(" ", 1)[0].rstrip(" ,;:.")
    if not cut:
        cut = text[: limit - 3]
    return cut + "..."


def _escape(text: str) -> str:
    """Make a label safe to sit inside a quoted Mermaid node."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = cleaned.replace('"', "'")
    # Mermaid treats these as markup inside labels even when quoted.
    cleaned = cleaned.replace("#", "no. ").replace("<", "(").replace(">", ")")
    return _shorten(cleaned, LABEL_LIMIT)


def _node(step: Step) -> str:
    open_shape, close_shape = SHAPES.get(step.kind, SHAPES["task"])
    label = _escape(step.text)
    if step.actor:
        label = _shorten(f"{_escape(step.actor)}: {label}", LABEL_LIMIT)
    return f'{step.id}{open_shape}"{label}"{close_shape}'


def _legend_lines(process_map: ProcessMap) -> list[str]:
    """A key for the shapes that actually appear on this map.

    Without it the shapes are a private code. Only the kinds used are listed, so the
    key never explains something the reader cannot see.
    """
    kinds: list[str] = []
    for step in process_map.steps:
        if step.kind not in kinds:
            kinds.append(step.kind)
    if len(kinds) < 2:
        return []

    lines = ['    subgraph legend["What the shapes mean"]', "        direction LR"]
    for index, kind in enumerate(kinds, start=1):
        open_shape, close_shape = SHAPES.get(kind, SHAPES["task"])
        meaning = KIND_MEANINGS.get(kind, kind.replace("_", " ").capitalize())
        lines.append(f'        L{index}{open_shape}"{meaning}"{close_shape}')
    lines.append("    end")
    return lines


def render_mermaid(
    process_map: ProcessMap, direction: str = "TD", legend: bool = True
) -> str:
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

    if legend:
        lines.extend(_legend_lines(process_map))

    return "\n".join(lines)
