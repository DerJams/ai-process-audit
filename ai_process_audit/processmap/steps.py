"""Infer structured steps from a free text process description.

Deterministic. No model is consulted here.

This is deliberately a shallow rule based reader, not an attempt at understanding.
It splits the description on sentence and ordering boundaries, then labels each
fragment by looking for known verbs. It will get things wrong on badly written
descriptions, and that is acceptable, because the map is shown to a human as a
draft of what the engine thinks the process is. If the map is wrong, the reader can
see immediately that the intake was unclear, which is useful information in itself.

What this module does not do: infer branches, loops, or parallel paths. A decision
step is marked as a decision, but its branches are not guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..model.models import Business, Process

# Words that mark the start of a new step inside a sentence. Ordered longest first
# so that "and then" is consumed before "then".
CONNECTORS = [
    "and then",
    "after that",
    "at which point",
    "once that",
    "before that",
    "then",
    "next",
    "afterwards",
    "finally",
    "meanwhile",
    "eventually",
]

_CONNECTOR_PATTERN = re.compile(
    r"(?:^|[,;]\s*|\s+)(?:" + "|".join(re.escape(word) for word in CONNECTORS) + r")\b[,\s]*",
    re.IGNORECASE,
)

_SENTENCE_PATTERN = re.compile(r"(?<=[.!?])\s+|\n+")

# Step kinds in priority order. The first kind whose keywords appear in a fragment
# wins, so the order encodes which reading matters most when a fragment does several
# things at once. Approval beats communication because "emails it for sign off" is
# better understood as an approval gate than as sending a message.
KIND_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("decision", ("if ", "whether", "depending on", "depends on", "decide", "unless", "either")),
    ("wait", ("wait", "waits", "waiting", "until", "sits in", "sits on", "queue", "backlog")),
    (
        "approval",
        (
            "approve", "approves", "approval", "sign off", "signs off", "authorise",
            "authorize", "review", "reviews", "check", "checks", "verify", "verifies",
            "reconcile", "reconciles", "match against", "cross reference",
        ),
    ),
    (
        "data_entry",
        (
            "type ", "types ", "typing", "enter", "enters", "copy", "copies", "paste",
            "pastes", "re-key", "rekey", "transcribe", "fill in", "fills in",
            "write down", "writes down", "writes out", "hand write", "handwrite",
        ),
    ),
    (
        "communication",
        (
            "email", "emails", "call", "calls", "phone", "phones", "send", "sends",
            "reply", "replies", "notify", "notifies", "chase", "chases", "follow up",
            "follows up", "message", "messages", "text ", "texts ", "confirm with",
        ),
    ),
    (
        "system",
        (
            "export", "exports", "import", "imports", "upload", "uploads", "download",
            "downloads", "generate", "generates", "print", "prints", "scan", "scans",
            "sync", "syncs", "log in", "logs in", "logs into", "save", "saves",
            "file ", "files ", "record", "records", "update", "updates", "raise",
            "raises", "create", "creates",
        ),
    ),
]

# Actors that are outside the business. Used so the map can show handoffs to people
# the business does not control, which are usually where the delays are.
EXTERNAL_ACTORS = (
    "customer", "customers", "client", "clients", "supplier", "suppliers", "vendor",
    "patient", "patients", "guest", "guests", "tenant", "tenants", "candidate",
    "candidates", "accountant", "bookkeeper", "insurer", "council", "landlord",
)

# Tool words worth spotting even when a business did not list them as tools.
GENERIC_TOOLS = (
    "paper", "spreadsheet", "spreadsheets", "email", "inbox", "phone", "whatsapp",
    "text message", "notebook", "whiteboard", "diary", "post-it", "pdf", "fax",
)

# Fragments shorter than this many words are treated as sentence debris, not steps.
MIN_WORDS_PER_STEP = 3

# An actor is only recognised when it is the subject at the very start of a fragment.
# Anything looser reads "emails the customer" as the customer doing the emailing, and
# "she messages the engineer" as the engineer doing the messaging. Both are wrong and
# both look careless on a map a client is reading. Most fragments therefore end up
# with no actor at all, which is the honest answer, because English drops the subject
# after the first clause and this parser cannot recover it.
LEADING_FILLER = frozenset(
    {"the", "a", "an", "each", "every", "one", "any", "all", "our", "their", "his", "her"}
)

MAX_STEPS = 24


@dataclass(frozen=True)
class Step:
    """One inferred step in a process."""

    index: int
    id: str
    text: str
    kind: str
    actor: str | None = None
    actor_is_external: bool = False
    tools: tuple[str, ...] = ()

    @property
    def is_manual(self) -> bool:
        """Steps a person performs by hand, which are the automation surface."""
        return self.kind in {"data_entry", "communication", "approval", "task"}


@dataclass(frozen=True)
class ProcessMap:
    """The inferred shape of one process, plus counts the report and scorer use."""

    process_id: str
    process_name: str
    steps: tuple[Step, ...]
    truncated: bool = False

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def manual_step_count(self) -> int:
        return sum(1 for step in self.steps if step.is_manual)

    @property
    def data_entry_count(self) -> int:
        return sum(1 for step in self.steps if step.kind == "data_entry")

    @property
    def decision_count(self) -> int:
        return sum(1 for step in self.steps if step.kind == "decision")

    @property
    def wait_count(self) -> int:
        return sum(1 for step in self.steps if step.kind == "wait")

    @property
    def handoff_count(self) -> int:
        """Number of times the process changes hands between named actors."""
        seen = [step.actor for step in self.steps if step.actor]
        return sum(1 for before, after in zip(seen, seen[1:]) if before != after)

    @property
    def tools_touched(self) -> tuple[str, ...]:
        found: list[str] = []
        for step in self.steps:
            for tool in step.tools:
                if tool not in found:
                    found.append(tool)
        return tuple(found)


def _split_fragments(description: str) -> list[str]:
    fragments: list[str] = []
    for sentence in _SENTENCE_PATTERN.split(description):
        sentence = sentence.strip()
        if not sentence:
            continue
        for piece in _CONNECTOR_PATTERN.split(sentence):
            piece = piece.strip(" ,;.\t")
            if piece:
                fragments.append(piece)
    return fragments


def _classify(fragment: str) -> str:
    lowered = f" {fragment.lower()} "
    for kind, keywords in KIND_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return kind
    return "task"


def _subject_of(fragment: str) -> str:
    """Return the fragment with any leading article or quantifier removed."""
    words = fragment.lower().split()
    while words and words[0].strip(",") in LEADING_FILLER:
        words.pop(0)
    return " ".join(words)


def _starts_with_actor(subject: str, actor: str) -> bool:
    # A trailing s is allowed so that "Engineers fill in" matches the role engineer.
    return re.match(rf"{re.escape(actor.lower())}s?\b", subject) is not None


def _find_actor(fragment: str, known_roles: tuple[str, ...]) -> tuple[str | None, bool]:
    subject = _subject_of(fragment)
    # Prefer the roles the intake actually named, longest first so that
    # "office manager" wins over "manager".
    for role in sorted(known_roles, key=len, reverse=True):
        if role and _starts_with_actor(subject, role):
            return role, False
    for actor in EXTERNAL_ACTORS:
        if _starts_with_actor(subject, actor):
            return actor.rstrip("s").capitalize(), True
    return None, False


def _find_tools(fragment: str, candidates: tuple[str, ...]) -> tuple[str, ...]:
    lowered = fragment.lower()
    found: list[str] = []
    for tool in sorted(candidates, key=len, reverse=True):
        if not tool:
            continue
        if tool.lower() in lowered and tool not in found:
            # Skip a tool already covered by a longer name, for example do not add
            # "Xero" when "Xero Payroll" was already matched.
            if any(tool.lower() in existing.lower() for existing in found):
                continue
            found.append(tool)
    return tuple(found)


def _tidy(fragment: str) -> str:
    text = re.sub(r"\s+", " ", fragment).strip()
    if text:
        text = text[0].upper() + text[1:]
    return text


def build_process_map(process: Process, business: Business | None = None) -> ProcessMap:
    """Infer the steps of a process from its description."""
    known_roles = process.people.roles
    tool_candidates = tuple(process.current_tools)
    if business is not None:
        tool_candidates += tuple(business.tools_in_use)
    tool_candidates += GENERIC_TOOLS

    fragments = _split_fragments(process.description)
    steps: list[Step] = []
    for fragment in fragments:
        if len(fragment.split()) < MIN_WORDS_PER_STEP:
            continue
        if len(steps) >= MAX_STEPS:
            return ProcessMap(process.id, process.name, tuple(steps), truncated=True)
        actor, is_external = _find_actor(fragment, known_roles)
        index = len(steps) + 1
        steps.append(
            Step(
                index=index,
                id=f"S{index}",
                text=_tidy(fragment),
                kind=_classify(fragment),
                actor=actor,
                actor_is_external=is_external,
                tools=_find_tools(fragment, tool_candidates),
            )
        )

    if not steps:
        # A description that survives schema validation but yields no usable
        # fragments still needs a map, so fall back to a single step holding the
        # whole description. The report shows this as a one step map, which reads
        # as the warning it is.
        steps.append(
            Step(
                index=1,
                id="S1",
                text=_tidy(process.description)[:200],
                kind=_classify(process.description),
                actor=known_roles[0] if known_roles else None,
                tools=_find_tools(process.description, tool_candidates),
            )
        )

    return ProcessMap(process.id, process.name, tuple(steps))
