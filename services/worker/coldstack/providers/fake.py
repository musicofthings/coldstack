"""Deterministic fake providers.

Two jobs: make the pipeline testable without spending anyone's credits, and let a new
self-hoster run the full search -> enrich -> verify -> CSV path before they have signed
up for anything. Seeded, so the same query always produces the same people.
"""
from __future__ import annotations

import hashlib
import random

from .base import (CompanyRec, PersonRec, ProviderCost, SearchQuery, SearchResult,
                   VerifyResult)
from .registry import register

_FIRST = ["Ana", "Ben", "Chen", "Divya", "Eli", "Farah", "Gita", "Hugo", "Ivan", "Jia",
          "Kwame", "Lena", "Mateo", "Nadia", "Omar", "Priya", "Quinn", "Rosa", "Sami", "Tara"]
_LAST = ["Ahmed", "Bianchi", "Cruz", "Devi", "Eriksen", "Fontaine", "Gupta", "Haddad",
         "Ito", "Jensen", "Kaur", "Lopez", "Mwangi", "Novak", "Okafor", "Patel"]
_COMPANIES = [("northwind-labs.example", "Northwind Labs", "Biotechnology", 45),
              ("meridian-dx.example", "Meridian Diagnostics", "Diagnostics", 120),
              ("kestrel-bio.example", "Kestrel Bio", "Biotechnology", 18),
              ("axiom-health.example", "Axiom Health", "Healthcare", 340),
              ("lumen-genomics.example", "Lumen Genomics", "Genomics", 72)]
_TITLES = ["Head of Laboratory", "VP Clinical Operations", "Director of Genomics",
           "Chief Scientific Officer", "Lab Manager", "Director of Bioinformatics"]


def _rng(q: SearchQuery) -> random.Random:
    seed = hashlib.sha256(repr(sorted(q.__dict__ if hasattr(q, "__dict__") else
                                      [(f, getattr(q, f)) for f in q.__slots__])).encode())
    return random.Random(seed.hexdigest())


@register("search")
class FakeSearch:
    name = "fake"

    async def search(self, q: SearchQuery, *, key: str = "") -> SearchResult:
        r = _rng(q)
        people = []
        for i in range(min(q.limit, 500)):
            dom, cname, industry, heads = _COMPANIES[r.randrange(len(_COMPANIES))]
            fn, ln = _FIRST[r.randrange(len(_FIRST))], _LAST[r.randrange(len(_LAST))]
            people.append(PersonRec(
                full_name=f"{fn} {ln}", first_name=fn, last_name=ln,
                title=(q.titles[i % len(q.titles)] if q.titles
                       else _TITLES[r.randrange(len(_TITLES))]),
                seniority=r.choice(["c_suite", "vp", "director", "manager"]),
                department="research",
                email=None,                       # like Apollo: search reveals no email
                linkedin_url=f"https://linkedin.com/in/{fn.lower()}-{ln.lower()}-{i}",
                company=CompanyRec(domain=dom, name=cname, industry=industry,
                                   headcount=heads, country="US", tech_stack=["HubSpot"]),
                raw={"id": f"fake_{i}"},
            ))
        return SearchResult(people=people, total_available=len(people),
                            cost=ProviderCost(1, 0.0, hit=True, latency_ms=1))

    async def test_key(self, key: str = "") -> bool:
        return True


@register("finder")
class FakeFinder:
    name = "fake"
    est_unit_cost_usd = 0.004

    async def find_email(self, person: PersonRec, *, key: str = "") -> tuple[str | None, ProviderCost]:
        dom = person.company.domain if person.company else None
        if not dom or not person.first_name:
            return None, ProviderCost(0, 0.0, hit=False)
        # ~78% hit rate, deterministic per person
        h = int(hashlib.sha256(f"{person.full_name}{dom}".encode()).hexdigest(), 16)
        if h % 100 >= 78:
            return None, ProviderCost(1, self.est_unit_cost_usd, hit=False, latency_ms=2)
        email = f"{person.first_name}.{person.last_name}@{dom}".lower()
        return email, ProviderCost(1, self.est_unit_cost_usd, hit=True, latency_ms=2)

    async def test_key(self, key: str = "") -> bool:
        return True


@register("verifier")
class FakeVerifier:
    name = "fake"
    est_unit_cost_usd = 0.0005

    async def verify(self, email: str, *, key: str | None = None) -> VerifyResult:
        h = int(hashlib.sha256(email.encode()).hexdigest(), 16) % 100
        verdict = "valid" if h < 80 else "catch_all" if h < 90 else "risky" if h < 96 else "invalid"
        return VerifyResult(email=email, verdict=verdict, mx_found=True,
                            smtp_accepts=verdict == "valid", is_disposable=False,
                            cost=ProviderCost(1, self.est_unit_cost_usd, hit=True, latency_ms=1))
