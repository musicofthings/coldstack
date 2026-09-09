"""Apollo.io — discovery (search) and enrichment (email reveal).

Two distinct operations with very different economics, which is why they are two
adapters rather than one:

  search      POST /api/v1/mixed_people/search   - returns people, NO email addresses.
              Apollo masks them as `email_not_unlocked@domain.com`. Cheap/free against
              your plan's search quota. Hard ceiling: 100 per page, 500 pages, so no
              single query can page past 50,000 records - split the query instead.

  enrichment  POST /api/v1/people/match          - reveals the email, 1+ credits each,
              charged only when data is found. Bulk variant takes at most 10 people.

Because reveal costs credits, Apollo enrichment is registered as one finder among
several in the waterfall, not the default path. A dedicated finder is often cheaper
per resolved email than an Apollo credit - which is exactly what the cost ledger
measures rather than assumes.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from .base import (CompanyRec, PersonRec, ProviderCost, SearchQuery, SearchResult)
from .registry import register

BASE = "https://api.apollo.io/api/v1"
MASKED = "email_not_unlocked"
MAX_PER_PAGE = 100
MAX_PAGES = 500

# Apollo takes headcount as banded strings, not a min/max pair.
_BANDS = [(1, 10), (11, 20), (21, 50), (51, 100), (101, 200),
          (201, 500), (501, 1000), (1001, 2000), (2001, 5000),
          (5001, 10000), (10001, 1000000)]


def _headcount_bands(lo: int | None, hi: int | None) -> list[str]:
    if lo is None and hi is None:
        return []
    lo, hi = lo or 1, hi or 1_000_000
    return [f"{a},{b}" for a, b in _BANDS if b >= lo and a <= hi]


def _headers(key: str) -> dict[str, str]:
    return {"x-api-key": key, "Content-Type": "application/json",
            "Cache-Control": "no-cache", "accept": "application/json"}


def _company(org: dict[str, Any] | None) -> CompanyRec | None:
    if not org:
        return None
    return CompanyRec(
        domain=org.get("primary_domain") or org.get("website_url"),
        name=org.get("name"),
        linkedin_url=org.get("linkedin_url"),
        industry=org.get("industry"),
        headcount=org.get("estimated_num_employees"),
        country=org.get("country"),
        city=org.get("city"),
        tech_stack=org.get("technology_names") or [],
        raw=org,
    )


def _person(p: dict[str, Any]) -> PersonRec:
    email = p.get("email")
    if email and MASKED in email:
        email = None                      # masked placeholder, not an address
    depts = p.get("departments") or []
    return PersonRec(
        full_name=p.get("name"),
        first_name=p.get("first_name"),
        last_name=p.get("last_name"),
        title=p.get("title"),
        seniority=p.get("seniority"),
        department=depts[0] if depts else None,
        email=email,
        linkedin_url=p.get("linkedin_url"),
        company=_company(p.get("organization")),
        raw=p,
    )


@register("search")
class ApolloSearch:
    name = "apollo"

    async def search(self, q: SearchQuery, *, key: str) -> SearchResult:
        unsupported: list[str] = []
        if q.revenue_min or q.revenue_max:
            unsupported.append("revenue (Apollo exposes this only on paid org search)")

        body: dict[str, Any] = {
            "page": 1,
            "per_page": min(q.limit, MAX_PER_PAGE),
        }
        if q.titles:
            body["person_titles"] = q.titles
        if q.seniorities:
            body["person_seniorities"] = q.seniorities
        if q.departments:
            body["person_departments"] = q.departments
        if q.countries:
            body["person_locations"] = q.countries
        if q.industries:
            body["q_organization_keyword_tags"] = q.industries
        if q.keywords:
            body["q_keywords"] = " ".join(q.keywords)
        if q.tech_any:
            body["currently_using_any_of_technologies"] = q.tech_any
        bands = _headcount_bands(q.headcount_min, q.headcount_max)
        if bands:
            body["organization_num_employees_ranges"] = bands

        people: list[PersonRec] = []
        total: int | None = None
        pages_fetched = 0
        started = time.perf_counter()

        async with httpx.AsyncClient(timeout=45) as client:
            while len(people) < q.limit and pages_fetched < MAX_PAGES:
                resp = await client.post(f"{BASE}/mixed_people/search",
                                         headers=_headers(key), json=body)
                if resp.status_code == 429:
                    raise RuntimeError("apollo: rate limited (429)")
                resp.raise_for_status()
                data = resp.json()

                batch = data.get("people") or data.get("contacts") or []
                people.extend(_person(p) for p in batch)
                pagination = data.get("pagination") or {}
                total = pagination.get("total_entries", total)
                pages_fetched += 1

                if not batch or pagination.get("page", 1) >= pagination.get("total_pages", 1):
                    break
                body["page"] = body["page"] + 1

        if total and total > MAX_PER_PAGE * MAX_PAGES:
            unsupported.append(
                f"{total} matches exceed Apollo's 50,000-record paging ceiling - "
                "narrow the query and run it in slices"
            )

        return SearchResult(
            people=people[: q.limit],
            total_available=total,
            cost=ProviderCost(units=pages_fetched, unit_cost_usd=0.0, hit=bool(people),
                              latency_ms=int((time.perf_counter() - started) * 1000)),
            unsupported_filters=unsupported,
        )

    async def test_key(self, key: str) -> bool:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"{BASE}/mixed_people/search", headers=_headers(key),
                                  json={"page": 1, "per_page": 1})
        return r.status_code < 400


@register("finder")
class ApolloEnrich:
    """Email reveal. Credits are charged only when Apollo actually finds something."""
    name = "apollo_enrich"
    est_unit_cost_usd = 0.03          # ~1 credit; refined from the ledger in practice

    async def find_email(self, person: PersonRec, *, key: str) -> tuple[str | None, ProviderCost]:
        started = time.perf_counter()
        body: dict[str, Any] = {"reveal_personal_emails": False}
        if person.raw.get("id"):
            body["id"] = person.raw["id"]
        else:
            body["first_name"] = person.first_name
            body["last_name"] = person.last_name
            if person.company and person.company.domain:
                body["domain"] = person.company.domain
            if person.linkedin_url:
                body["linkedin_url"] = person.linkedin_url

        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(f"{BASE}/people/match", headers=_headers(key), json=body)

        latency = int((time.perf_counter() - started) * 1000)
        if resp.status_code >= 400:
            return None, ProviderCost(1, 0.0, hit=False, latency_ms=latency)

        matched = (resp.json() or {}).get("person") or {}
        email = matched.get("email")
        if email and MASKED in email:
            email = None
        return email, ProviderCost(
            units=1,
            unit_cost_usd=self.est_unit_cost_usd if email else 0.0,   # no charge on a miss
            hit=bool(email),
            latency_ms=latency,
        )

    async def test_key(self, key: str) -> bool:
        return await ApolloSearch().test_key(key)
