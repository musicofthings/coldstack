"""Sequence copy generation, with a linter that runs on output rather than trust.

An LLM asked for cold email copy reliably produces the things that get cold email
filtered: a subject in title case with an exclamation mark, "I hope this email finds
you well", a tracked link in the first touch, three paragraphs where one would do. So
generation is followed by `lint`, which is pure, deterministic, and the part worth
trusting.

The linter is also useful on hand-written copy, which is why it takes a string and not
a model response.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from ..sending.sequences import Sequence, Step, Variant
from .llm import Llm

MAX_BODY_WORDS = 120
MAX_SUBJECT_CHARS = 60

# Words and phrases with a measurable effect on placement, plus the clichés that mark
# a message as bulk to a human reader even when filters let them through.
_SPAM_PHRASES = [
    "act now", "limited time", "risk free", "risk-free", "100% free", "guarantee",
    "guaranteed", "no obligation", "click here", "buy now", "order now", "cash bonus",
    "double your", "earn extra", "extra income", "make money", "no credit check",
    "special promotion", "urgent", "winner", "congratulations you", "dear friend",
    "this is not spam", "unsubscribe below to stop",
]
_CLICHES = [
    "i hope this email finds you well", "hope you're doing well", "hope this finds you well",
    "i wanted to reach out", "just reaching out", "quick question for you",
    "touching base", "circling back", "per my last email", "as per my previous",
    "i'll keep this brief", "hope you don't mind me reaching out",
    "revolutionary", "game-changing", "game changing", "cutting-edge", "synergy",
    "leverage our", "best-in-class", "world-class solution",
]


class Severity(str, Enum):
    FAIL = "fail"
    WARN = "warn"
    INFO = "info"

    def __str__(self) -> str:
        return self.value


@dataclass(slots=True)
class LintFinding:
    severity: Severity
    check: str
    detail: str
    fix: str | None = None


@dataclass(slots=True)
class LintReport:
    findings: list[LintFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.severity is Severity.FAIL for f in self.findings)

    def summary(self) -> str:
        if not self.findings:
            return "clean"
        return "\n".join(f"[{f.severity}] {f.check}: {f.detail}" for f in self.findings)


def lint(subject: str, body: str, *, step_index: int = 0) -> LintReport:
    r = LintReport()
    low_body, low_subject = body.lower(), subject.lower()
    words = body.split()

    if len(words) > MAX_BODY_WORDS:
        r.findings.append(LintFinding(
            Severity.WARN, "length", f"{len(words)} words",
            f"Cold email replies fall off sharply past ~{MAX_BODY_WORDS} words. Cut to one ask."))

    if subject and len(subject) > MAX_SUBJECT_CHARS:
        r.findings.append(LintFinding(
            Severity.WARN, "subject length", f"{len(subject)} characters",
            "Mobile clients truncate around 40-60 characters."))

    if subject and subject == subject.title() and len(subject.split()) > 2:
        r.findings.append(LintFinding(
            Severity.WARN, "subject case", "Title Case reads as marketing",
            "Write it like a person would type it: sentence case."))

    if "!" in subject:
        r.findings.append(LintFinding(
            Severity.FAIL, "subject punctuation", "exclamation mark in the subject",
            "One of the strongest single spam signals in a subject line."))

    caps = [w for w in re.findall(r"\b[A-Z]{4,}\b", body) if w not in ("ASAP", "SaaS")]
    if caps:
        r.findings.append(LintFinding(
            Severity.WARN, "shouting", f"all-caps words: {caps[:3]}", "Use normal case."))

    if re.search(r"[!?]{2,}", body) or body.count("!") > 1:
        r.findings.append(LintFinding(
            Severity.WARN, "punctuation", "repeated or multiple exclamation marks", None))

    found_spam = [p for p in _SPAM_PHRASES if p in low_body or p in low_subject]
    if found_spam:
        r.findings.append(LintFinding(
            Severity.FAIL, "spam phrases", f"{found_spam[:4]}",
            "These measurably hurt placement. Rewrite without them."))

    found_cliche = [p for p in _CLICHES if p in low_body or p in low_subject]
    if found_cliche:
        r.findings.append(LintFinding(
            Severity.WARN, "cliché", f"{found_cliche[:3]}",
            "A human reader reads these as bulk mail even when filters do not."))

    links = re.findall(r"https?://\S+", body)
    if links and step_index == 0:
        r.findings.append(LintFinding(
            Severity.FAIL, "link in first touch", f"{len(links)} link(s)",
            "A link in a first cold email is a strong filtering signal and there is no "
            "reason for one before a reply. Move it to a later step."))
    elif len(links) > 1:
        r.findings.append(LintFinding(
            Severity.WARN, "links", f"{len(links)} links", "One at most."))

    if re.search(r"<img|\[image|cid:", low_body):
        r.findings.append(LintFinding(
            Severity.WARN, "image", "embedded image",
            "Images in cold email suppress deliverability and are usually blocked anyway."))

    unresolved = re.findall(r"\{\{(\w+)\}\}", body + " " + subject)
    if unresolved:
        r.findings.append(LintFinding(
            Severity.INFO, "tokens", f"placeholders present: {sorted(set(unresolved))}",
            "Confirm every lead in the list has these fields, or the recipient sees the "
            "raw token."))

    asks = len(re.findall(r"\?", body))
    if asks > 2:
        r.findings.append(LintFinding(
            Severity.WARN, "multiple asks", f"{asks} questions",
            "One question gets answered; three get ignored."))

    return r


SYSTEM = """You write cold outreach sequences that read as if one person typed them.

Hard rules:
- Under 90 words per email. One question, at the end.
- Sentence case subjects, under 50 characters, no exclamation marks.
- No links in the first email.
- Never write "I hope this email finds you well", "just reaching out", "touching base",
  "circling back", or similar filler.
- No claims about the recipient you were not given. No invented metrics.
- Plain text. No marketing adjectives.

Return ONLY JSON:
{"steps":[{"delay_days":0,"variants":[{"label":"A","subject":"...","body":"..."}]}]}
Step 1 has delay_days 0. Later steps follow up in the same thread and may omit the
subject (use null)."""


@dataclass(slots=True)
class GeneratedSequence:
    sequence: Sequence | None
    lint_reports: list[tuple[int, str, LintReport]] = field(default_factory=list)
    ok: bool = True
    error: str | None = None

    @property
    def has_failures(self) -> bool:
        return any(not r.ok for _, _, r in self.lint_reports)


async def generate(offer: str, audience: str, llm: Llm, *, steps: int = 3,
                   variants: int = 2) -> GeneratedSequence:
    prompt = (f"OFFER: {offer}\nAUDIENCE: {audience}\n\n"
              f"Write {steps} steps with {variants} variant(s) each.")
    res = await llm.complete(prompt, system=SYSTEM, max_tokens=2000, temperature=0.7)
    if not res.ok:
        return GeneratedSequence(None, ok=False, error=res.error)

    data = res.json()
    raw_steps = (data or {}).get("steps") if isinstance(data, dict) else None
    if not isinstance(raw_steps, list) or not raw_steps:
        return GeneratedSequence(None, ok=False, error="model returned no usable steps")

    built: list[Step] = []
    reports: list[tuple[int, str, LintReport]] = []
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            continue
        vs: list[Variant] = []
        for j, rv in enumerate(raw.get("variants") or []):
            if not isinstance(rv, dict) or not rv.get("body"):
                continue
            label = str(rv.get("label") or chr(65 + j))
            subject = rv.get("subject") or None
            body = str(rv["body"])
            vs.append(Variant(id=f"s{i}{label}", label=label, subject=subject, body=body))
            reports.append((i, label, lint(subject or "", body, step_index=i)))
        if vs:
            built.append(Step(index=i, variants=vs,
                              delay_days=int(raw.get("delay_days", 0 if i == 0 else 3)),
                              same_thread=i > 0))

    if not built:
        return GeneratedSequence(None, ok=False, error="no variant had a body")
    return GeneratedSequence(Sequence(steps=built), lint_reports=reports)
