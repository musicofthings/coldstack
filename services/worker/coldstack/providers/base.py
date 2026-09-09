"""Provider adapter contract.

Every data vendor — search, email-find, verify, enrich — implements one of these
protocols. Nothing above this layer may import a vendor SDK directly.

Two rules that keep the abstraction honest:
  1. Adapters return normalised dataclasses AND the raw payload. The raw payload is
     persisted to `provider_record` so we can re-project without re-buying.
  2. Adapters declare their cost. The waterfall orders by observed cost-per-hit, and
     that only works if every call reports units and price.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable


@dataclass(slots=True)
class ProviderCost:
    units: int = 1
    unit_cost_usd: float = 0.0
    hit: bool | None = None
    latency_ms: int | None = None


@dataclass(slots=True)
class CompanyRec:
    domain: str | None = None
    name: str | None = None
    linkedin_url: str | None = None
    industry: str | None = None
    headcount: int | None = None
    country: str | None = None
    city: str | None = None
    tech_stack: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PersonRec:
    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    seniority: str | None = None
    department: str | None = None
    email: str | None = None
    linkedin_url: str | None = None
    company: CompanyRec | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchQuery:
    """Vendor-neutral query. The AI layer compiles plain-English ICP text into this;
    each adapter lowers it into its own filter dialect and reports what it dropped."""
    titles: list[str] = field(default_factory=list)
    seniorities: list[str] = field(default_factory=list)
    departments: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    headcount_min: int | None = None
    headcount_max: int | None = None
    revenue_min: int | None = None
    revenue_max: int | None = None
    tech_any: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    exclude_domains: list[str] = field(default_factory=list)
    limit: int = 100


@dataclass(slots=True)
class SearchResult:
    people: list[PersonRec]
    total_available: int | None
    cost: ProviderCost
    unsupported_filters: list[str] = field(default_factory=list)


@dataclass(slots=True)
class VerifyResult:
    email: str
    verdict: str          # valid | invalid | catch_all | risky | unknown
    mx_found: bool | None = None
    smtp_accepts: bool | None = None
    is_disposable: bool | None = None
    is_role_account: bool | None = None
    cost: ProviderCost = field(default_factory=ProviderCost)
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SearchProvider(Protocol):
    name: str
    async def search(self, q: SearchQuery, *, key: str) -> SearchResult: ...
    async def test_key(self, key: str) -> bool: ...


@runtime_checkable
class EmailFinder(Protocol):
    name: str
    est_unit_cost_usd: float
    async def find_email(self, person: PersonRec, *, key: str) -> tuple[str | None, ProviderCost]: ...
    async def test_key(self, key: str) -> bool: ...


@runtime_checkable
class Verifier(Protocol):
    name: str
    est_unit_cost_usd: float
    async def verify(self, email: str, *, key: str | None = None) -> VerifyResult: ...


@runtime_checkable
class SignalSource(Protocol):
    name: str
    kind: str             # hiring | tech_change | funding | job_change | github | news
    async def observe(self, company: CompanyRec, *, key: str | None = None) -> Iterable[dict[str, Any]]: ...
