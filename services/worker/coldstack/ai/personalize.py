"""Personalisation that cannot fabricate.

The defining failure of AI cold email is the invented compliment: "loved your post on
scaling Postgres" when no such post exists. It is worse than no personalisation - it
tells the recipient the sender is automated and careless, and it is unrecoverable.

So personalisation here is **citation-gated**. The model is given a numbered list of
verified signals, each with a source URL, and must return lines that each cite the
signal ids they rest on. Afterwards, every returned line is checked:

  1. it cites at least one signal id that we actually supplied
  2. every number in the line appears in the cited signals
  3. every capitalised term in the line appears in the cited signals, the lead's own
     record, or a small allowlist of ordinary sentence-initial words

A line failing any check is discarded, not repaired. With no signals at all the function
returns None and the caller uses its generic opener - because "no personalisation" is a
perfectly good outcome and an invented one is not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .llm import Llm

MAX_WORDS = 40

SYSTEM = """You write one or two sentences of opening personalisation for a cold email.

You may ONLY state facts contained in the numbered SIGNALS given to you. You may not
add industry commentary, guesses, flattery about things not in the signals, or any
number, name, product or date that does not appear there.

Return ONLY JSON: {"lines": [{"text": "...", "cites": [1, 2]}]}
Each line must cite the signal numbers it rests on. If the signals do not support
anything worth saying, return {"lines": []}. An empty answer is correct and expected."""

_SENTENCE_STARTERS = {
    "I", "We", "You", "Your", "The", "A", "An", "It", "They", "Since", "After",
    "Saw", "Noticed", "Given", "With", "As", "Congratulations", "Congrats", "Nice",
    "Looks", "Seems", "That", "This", "Both", "Most", "Many", "Also", "And", "But",
    "If", "When", "While", "So", "Just", "Thanks", "Hope", "Happy", "Monday",
    "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January",
    "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December",
}


@dataclass(slots=True)
class Signal:
    id: int
    summary: str
    source_url: str | None = None
    kind: str = "generic"


@dataclass(slots=True)
class PersonalisationResult:
    text: str | None
    cited: list[Signal] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)   # (line, why)
    used_fallback: bool = False

    @property
    def sources(self) -> list[str]:
        return [s.source_url for s in self.cited if s.source_url]


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\b\d[\d,]*\b", text)}


def _proper_nouns(text: str) -> set[str]:
    # Capitalised words that are not ordinary sentence openers.
    return {w for w in re.findall(r"\b[A-Z][A-Za-z0-9.+&-]{1,}\b", text)
            if w not in _SENTENCE_STARTERS}


def verify_line(line: str, cites: list[int], signals: dict[int, Signal],
                lead_terms: set[str]) -> tuple[bool, str]:
    """Pure and testable. This is the gate that makes the feature safe."""
    cited = [signals[c] for c in cites if c in signals]
    if not cited:
        return False, "cites no supplied signal"

    if len(line.split()) > MAX_WORDS:
        return False, f"longer than {MAX_WORDS} words"

    grounded = " ".join(s.summary for s in cited) + " " + " ".join(lead_terms)

    unsupported_numbers = _numbers(line) - _numbers(grounded)
    if unsupported_numbers:
        return False, f"numbers not in the cited signals: {sorted(unsupported_numbers)}"

    grounded_nouns = _proper_nouns(grounded) | {t for t in lead_terms}
    unsupported = {n for n in _proper_nouns(line)
                   if n not in grounded_nouns
                   and not any(n.lower() in g.lower() for g in grounded_nouns)}
    if unsupported:
        return False, f"names not in the cited signals: {sorted(unsupported)}"

    return True, ""


async def personalise(lead: dict[str, str], signals: list[Signal], llm: Llm,
                      *, fallback: str | None = None) -> PersonalisationResult:
    if not signals:
        # Correct outcome, not a failure. Nothing true to say means say nothing.
        return PersonalisationResult(fallback, used_fallback=True)

    by_id = {s.id: s for s in signals}
    lead_terms = {str(v) for v in lead.values() if v} | {
        w for v in lead.values() if v for w in str(v).split()}

    listing = "\n".join(
        f"{s.id}. [{s.kind}] {s.summary}" + (f" (source: {s.source_url})" if s.source_url else "")
        for s in signals)
    prompt = (f"LEAD: {lead.get('full_name', '')}, {lead.get('title', '')} at "
              f"{lead.get('company', '')}\n\nSIGNALS:\n{listing}")

    res = await llm.complete(prompt, system=SYSTEM, max_tokens=400, temperature=0.2)
    if not res.ok:
        return PersonalisationResult(fallback, used_fallback=True)

    data = res.json()
    lines = (data or {}).get("lines") if isinstance(data, dict) else None
    if not isinstance(lines, list):
        return PersonalisationResult(fallback, used_fallback=True)

    kept, cited, rejected = [], [], []
    for item in lines:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        raw_cites = item.get("cites") or []
        cites = [c for c in raw_cites if isinstance(c, int)]
        if not text:
            continue
        ok, why = verify_line(text, cites, by_id, lead_terms)
        if ok:
            kept.append(text)
            cited.extend(by_id[c] for c in cites if c in by_id)
        else:
            rejected.append((text, why))

    if not kept:
        # Everything failed verification. Fall back rather than sending an unverified
        # claim - a discarded line costs nothing, a fabricated one costs the reply.
        return PersonalisationResult(fallback, rejected=rejected, used_fallback=True)

    seen, unique_cites = set(), []
    for s in cited:
        if s.id not in seen:
            seen.add(s.id)
            unique_cites.append(s)
    return PersonalisationResult(" ".join(kept), cited=unique_cites, rejected=rejected)
