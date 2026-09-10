"""Plain-English ICP -> structured SearchQuery.

"Marketing agencies in the US with 10 to 50 employees" is what a user types; a provider
needs `person_titles`, `organization_num_employees_ranges`, `person_locations`. The LLM
does that translation, and then we **validate it against what SearchQuery can actually
represent** and report what we dropped.

That validation step is the whole point. A model asked for filters will happily invent
`funding_stage` or `uses_competitor`, and passing those through means either a provider
error or - worse - a silently ignored filter, so the user believes they targeted
Series-A companies and paid to enrich everybody.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..providers.base import SearchQuery
from .llm import Llm

# Only these fields exist on SearchQuery. Anything else the model emits is dropped.
_LIST_FIELDS = {"titles", "seniorities", "departments", "industries", "countries",
                "tech_any", "keywords", "exclude_domains"}
_INT_FIELDS = {"headcount_min", "headcount_max", "revenue_min", "revenue_max", "limit"}
_ALLOWED = _LIST_FIELDS | _INT_FIELDS

_SENIORITIES = {"owner", "founder", "c_suite", "partner", "vp", "head", "director",
                "manager", "senior", "entry", "intern"}

SYSTEM = """You translate a described ideal customer profile into a structured search query.
Return ONLY a JSON object. Use ONLY these keys:
  titles, seniorities, departments, industries, countries, tech_any, keywords,
  exclude_domains  (arrays of strings)
  headcount_min, headcount_max, revenue_min, revenue_max  (integers)
seniorities must come from: owner, founder, c_suite, partner, vp, head, director,
manager, senior, entry, intern.
Do not invent keys. If the description mentions something none of these keys can
express, put it in keywords. Omit keys you have no value for."""


@dataclass(slots=True)
class IcpTranslation:
    query: SearchQuery
    dropped: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None

    def explain(self) -> str:
        lines = []
        for f in sorted(_ALLOWED):
            v = getattr(self.query, f, None)
            if v not in (None, [], 0):
                lines.append(f"  {f}: {v}")
        out = "interpreted as:\n" + ("\n".join(lines) or "  (no filters)")
        for w in self.warnings:
            out += f"\n  WARNING: {w}"
        return out


def validate(raw: dict, *, limit: int = 100) -> IcpTranslation:
    """Pure - no LLM. Separated so the validation rules are testable on their own."""
    query = SearchQuery(limit=limit)
    dropped: dict[str, object] = {}
    warnings: list[str] = []

    for key, value in (raw or {}).items():
        if key not in _ALLOWED:
            dropped[key] = value
            continue
        if key in _LIST_FIELDS:
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                dropped[key] = value
                continue
            clean = [str(v).strip() for v in value if str(v).strip()]
            if key == "seniorities":
                bad = [v for v in clean if v.lower() not in _SENIORITIES]
                clean = [v.lower() for v in clean if v.lower() in _SENIORITIES]
                if bad:
                    warnings.append(f"unsupported seniority values ignored: {bad}")
            setattr(query, key, clean)
        else:
            try:
                setattr(query, key, int(value))
            except (TypeError, ValueError):
                dropped[key] = value

    if dropped:
        warnings.append(
            "filters the query language cannot express were dropped: "
            f"{sorted(dropped)}. They were NOT applied - narrow manually or add them "
            "to keywords, otherwise you will pay to enrich people who do not match.")

    if (query.headcount_min and query.headcount_max
            and query.headcount_min > query.headcount_max):
        query.headcount_min, query.headcount_max = query.headcount_max, query.headcount_min
        warnings.append("headcount_min was above headcount_max; swapped")

    if not any([query.titles, query.seniorities, query.departments, query.industries,
                query.keywords, query.tech_any]):
        warnings.append("no person or company filter was produced - this would match "
                        "almost anyone. Add a title, industry or keyword.")

    return IcpTranslation(query=query, dropped=dropped, warnings=warnings)


async def translate(icp_text: str, llm: Llm, *, limit: int = 100) -> IcpTranslation:
    res = await llm.complete(f"Ideal customer profile:\n{icp_text}",
                             system=SYSTEM, max_tokens=700, temperature=0.0)
    if not res.ok:
        return IcpTranslation(SearchQuery(limit=limit), ok=False, error=res.error,
                              warnings=["LLM unavailable - fill the filters manually"])
    raw = res.json()
    if not isinstance(raw, dict):
        return IcpTranslation(SearchQuery(limit=limit), ok=False,
                              error="model did not return a JSON object",
                              warnings=["could not parse the model's response"])
    return validate(raw, limit=limit)
