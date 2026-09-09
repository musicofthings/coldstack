"""Additional email finders.

The waterfall only earns its keep when it has real competition to rank. These four
plus Hunter and Apollo give it six providers with genuinely different hit profiles:
Dropcontact is strongest on EU contacts and is the cleanest GDPR posture of the set;
LeadMagic and Findymail skew North American SaaS; Prospeo sits in between.

All four share the same shape - POST a name plus a company domain, get an email or a
miss - so they are one generic adapter driven by a spec, exactly like the ESPs.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .base import PersonRec, ProviderCost
from .registry import register


@dataclass(frozen=True, slots=True)
class FinderSpec:
    name: str
    url: str
    auth: Callable[[str], dict[str, str]]
    payload: Callable[[PersonRec, str], dict[str, Any]]
    extract: Callable[[dict[str, Any]], str | None]
    est_unit_cost_usd: float
    notes: str = ""


FINDYMAIL = FinderSpec(
    name="findymail",
    url="https://app.findymail.com/api/search/name",
    auth=lambda k: {"Authorization": f"Bearer {k}", "Content-Type": "application/json"},
    payload=lambda p, d: {"name": p.full_name, "domain": d},
    extract=lambda r: ((r.get("contact") or {}).get("email")),
    est_unit_cost_usd=0.008,
    notes="Charges only for verified finds.",
)

PROSPEO = FinderSpec(
    name="prospeo",
    url="https://api.prospeo.io/email-finder",
    auth=lambda k: {"X-KEY": k, "Content-Type": "application/json"},
    payload=lambda p, d: {"first_name": p.first_name, "last_name": p.last_name, "company": d},
    extract=lambda r: ((r.get("response") or {}).get("email")),
    est_unit_cost_usd=0.006,
)

LEADMAGIC = FinderSpec(
    name="leadmagic",
    url="https://api.leadmagic.io/email-finder",
    auth=lambda k: {"X-API-Key": k, "Content-Type": "application/json"},
    payload=lambda p, d: {"first_name": p.first_name, "last_name": p.last_name, "domain": d},
    extract=lambda r: r.get("email"),
    est_unit_cost_usd=0.004,
)

DROPCONTACT = FinderSpec(
    name="dropcontact",
    url="https://api.dropcontact.io/batch",
    auth=lambda k: {"X-Access-Token": k, "Content-Type": "application/json"},
    payload=lambda p, d: {"data": [{"first_name": p.first_name, "last_name": p.last_name,
                                    "website": d}], "siren": False, "language": "en"},
    # Dropcontact is async: this POST returns a request_id and the result is polled.
    # Treated as a miss on the first pass rather than blocking the waterfall; the
    # polling worker lands with the P1 batch enrichment queue.
    extract=lambda r: (((r.get("data") or [{}])[0].get("email") or [{}])[0].get("email")
                       if r.get("data") else None),
    est_unit_cost_usd=0.010,
    notes="EU-based, strongest GDPR posture. Async batch API - needs a polling worker.",
)

SPECS = {s.name: s for s in (FINDYMAIL, PROSPEO, LEADMAGIC, DROPCONTACT)}


class GenericFinder:
    def __init__(self, spec: FinderSpec) -> None:
        self.spec = spec
        self.name = spec.name
        self.est_unit_cost_usd = spec.est_unit_cost_usd

    async def find_email(self, person: PersonRec, *, key: str) -> tuple[str | None, ProviderCost]:
        domain = person.company.domain if person.company else None
        if not domain or not (person.first_name and person.last_name):
            return None, ProviderCost(units=0, unit_cost_usd=0.0, hit=False)

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(self.spec.url, headers=self.spec.auth(key),
                                         json=self.spec.payload(person, domain))
        except httpx.HTTPError:
            return None, ProviderCost(1, 0.0, hit=False)

        latency = int((time.perf_counter() - started) * 1000)
        if resp.status_code >= 400:
            return None, ProviderCost(1, 0.0, hit=False, latency_ms=latency)
        try:
            email = self.spec.extract(resp.json() or {})
        except (AttributeError, IndexError, TypeError, KeyError):
            email = None
        # Most of these bill only on a find, so a miss costs nothing.
        return email, ProviderCost(1, self.est_unit_cost_usd if email else 0.0,
                                   hit=bool(email), latency_ms=latency)

    async def test_key(self, key: str) -> bool:
        probe = PersonRec(first_name="Test", last_name="User", full_name="Test User")
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                resp = await client.post(self.spec.url, headers=self.spec.auth(key),
                                         json=self.spec.payload(probe, "example.com"))
            except httpx.HTTPError:
                return False
        return resp.status_code not in (401, 403)


def _make(spec: FinderSpec):
    cls = type(f"{spec.name.title()}Finder", (GenericFinder,), {
        "name": spec.name, "est_unit_cost_usd": spec.est_unit_cost_usd,
        "__init__": lambda self, _s=spec: GenericFinder.__init__(self, _s),
    })
    return register("finder")(cls)


for _spec in SPECS.values():
    _make(_spec)
