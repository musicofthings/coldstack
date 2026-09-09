"""The P0 vertical slice: ICP query -> search -> email waterfall -> verify -> rows.

Ordering is deliberate and is where most of the money is saved or wasted:

  1. search      one call, no emails returned (Apollo masks them)
  2. dedupe      by linkedin_url, then name+domain - before spending anything
  3. suppress    drop unsubscribes, bounces, competitors, existing customers - before
                 spending anything. Suppressing after enrichment means paying to
                 enrich people you were never allowed to contact.
  4. waterfall   cheapest-first email finding, stop on first hit
  5. verify      only addresses we actually found, cached by the caller
  6. budget      hard stop, checked between records, never mid-record

Steps 3 and 4 in that order is the single highest-leverage decision in the pipeline.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..enrich.waterfall import ProviderStats, find_email
from ..providers.base import (EmailFinder, PersonRec, SearchProvider, SearchQuery,
                              Verifier)


@dataclass(slots=True)
class Lead:
    full_name: str | None
    first_name: str | None
    last_name: str | None
    title: str | None
    seniority: str | None
    department: str | None
    company: str | None
    domain: str | None
    industry: str | None
    headcount: int | None
    country: str | None
    linkedin_url: str | None
    email: str | None = None
    email_source: str | None = None
    email_status: str = "unknown"
    cost_usd: float = 0.0


@dataclass(slots=True)
class BuildReport:
    searched: int = 0
    after_dedupe: int = 0
    after_suppression: int = 0
    emails_found: int = 0
    verified_valid: int = 0
    total_cost_usd: float = 0.0
    per_provider: dict[str, dict] = field(default_factory=dict)
    unsupported_filters: list[str] = field(default_factory=list)
    budget_exhausted: bool = False

    @property
    def cost_per_valid_email(self) -> float:
        return self.total_cost_usd / self.verified_valid if self.verified_valid else 0.0

    def summary(self) -> str:
        lines = [
            f"searched          {self.searched}",
            f"after dedupe      {self.after_dedupe}",
            f"after suppression {self.after_suppression}",
            f"emails found      {self.emails_found}",
            f"verified valid    {self.verified_valid}",
            f"total cost        ${self.total_cost_usd:.4f}",
            f"cost/valid email  ${self.cost_per_valid_email:.4f}",
        ]
        if self.per_provider:
            lines.append("providers:")
            for name, s in sorted(self.per_provider.items()):
                rate = s["hits"] / s["attempts"] if s["attempts"] else 0
                lines.append(f"  {name:16} {s['hits']:4}/{s['attempts']:<4} hits "
                             f"({rate:5.1%})  ${s['cost']:.4f}")
        if self.budget_exhausted:
            lines.append("NOTE: stopped early - budget exhausted")
        for w in self.unsupported_filters:
            lines.append(f"WARN: {w}")
        return "\n".join(lines)


def _dedupe(people: list[PersonRec]) -> list[PersonRec]:
    seen: set[str] = set()
    out: list[PersonRec] = []
    for p in people:
        dom = p.company.domain if p.company else ""
        key = (p.linkedin_url or "").lower() or f"{(p.full_name or '').lower()}|{(dom or '').lower()}"
        if key and key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _suppressed(p: PersonRec, emails: set[str], domains: set[str]) -> bool:
    dom = (p.company.domain if p.company else "") or ""
    if dom.lower() in domains:
        return True
    return bool(p.email and p.email.lower() in emails)


async def build_list(
    query: SearchQuery,
    *,
    search: SearchProvider,
    search_key: str,
    finders: dict[str, EmailFinder],
    finder_keys: dict[str, str],
    verifier: Verifier | None = None,
    verifier_key: str | None = None,
    suppressed_emails: set[str] | None = None,
    suppressed_domains: set[str] | None = None,
    budget_usd: float | None = None,
    concurrency: int = 8,
) -> tuple[list[Lead], BuildReport]:
    report = BuildReport()
    sup_e = {e.lower() for e in (suppressed_emails or set())}
    sup_d = {d.lower() for d in (suppressed_domains or set())}

    result = await search.search(query, key=search_key)
    report.searched = len(result.people)
    report.unsupported_filters = list(result.unsupported_filters)
    report.per_provider[f"search:{search.name}"] = {
        "attempts": result.cost.units, "hits": 1 if result.cost.hit else 0,
        "cost": result.cost.unit_cost_usd * result.cost.units}
    report.total_cost_usd += result.cost.unit_cost_usd * result.cost.units

    people = _dedupe(result.people)
    report.after_dedupe = len(people)
    people = [p for p in people if not _suppressed(p, sup_e, sup_d)]
    report.after_suppression = len(people)

    stats = {n: ProviderStats(unit_cost_usd=f.est_unit_cost_usd)
             for n, f in finders.items() if n in finder_keys}

    def _record(name: str, cost) -> None:
        slot = report.per_provider.setdefault(name, {"attempts": 0, "hits": 0, "cost": 0.0})
        slot["attempts"] += cost.units
        slot["hits"] += 1 if cost.hit else 0
        slot["cost"] += cost.unit_cost_usd * cost.units
        report.total_cost_usd += cost.unit_cost_usd * cost.units

    sem = asyncio.Semaphore(concurrency)
    remaining = {"budget": budget_usd}
    cheapest = min((f.est_unit_cost_usd for n, f in finders.items() if n in finder_keys),
                   default=0.0)

    async def _one(p: PersonRec) -> Lead:
        lead = Lead(
            full_name=p.full_name, first_name=p.first_name, last_name=p.last_name,
            title=p.title, seniority=p.seniority, department=p.department,
            company=p.company.name if p.company else None,
            domain=p.company.domain if p.company else None,
            industry=p.company.industry if p.company else None,
            headcount=p.company.headcount if p.company else None,
            country=p.company.country if p.company else None,
            linkedin_url=p.linkedin_url,
            email=p.email,
        )
        async with sem:
            if remaining["budget"] is not None and remaining["budget"] < cheapest:
                report.budget_exhausted = True
                return lead

            if not lead.email:
                email, trail = await find_email(p, finders, finder_keys, stats,
                                                budget_usd=remaining["budget"])
                for name, cost in trail:
                    _record(name, cost)
                    lead.cost_usd += cost.unit_cost_usd * cost.units
                    st = stats.get(name)
                    if st:
                        st.attempts += cost.units
                        st.hits += 1 if cost.hit else 0
                    if remaining["budget"] is not None:
                        remaining["budget"] -= cost.unit_cost_usd * cost.units
                    if cost.hit:
                        lead.email_source = name
                lead.email = email

            if lead.email and verifier:
                v = await verifier.verify(lead.email, key=verifier_key)
                _record(f"verify:{verifier.name}", v.cost)
                lead.cost_usd += v.cost.unit_cost_usd * v.cost.units
                if remaining["budget"] is not None:
                    remaining["budget"] -= v.cost.unit_cost_usd * v.cost.units
                lead.email_status = v.verdict
        return lead

    leads = await asyncio.gather(*(_one(p) for p in people))
    report.emails_found = sum(1 for l in leads if l.email)
    report.verified_valid = sum(1 for l in leads if l.email_status == "valid")
    return list(leads), report
