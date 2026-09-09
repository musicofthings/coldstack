"""Hunter.io — email finder and verifier.

Cheaper per resolved email than an Apollo credit in most cases, which is the whole
argument for the waterfall. Needs a company domain: Hunter resolves name + domain,
so a person with no company domain is skipped rather than guessed at.
"""
from __future__ import annotations

import time

import httpx

from .base import PersonRec, ProviderCost, VerifyResult
from .registry import register

BASE = "https://api.hunter.io/v2"

# Hunter's confidence score is 0-100. Below this we hand the address to the verifier
# rather than treating it as found, since a wrong address is a bounce and a bounce is
# reputation damage - far more expensive than the credit saved.
MIN_CONFIDENCE = 70


@register("finder")
class HunterFinder:
    name = "hunter"
    est_unit_cost_usd = 0.005

    async def find_email(self, person: PersonRec, *, key: str) -> tuple[str | None, ProviderCost]:
        domain = person.company.domain if person.company else None
        if not domain or not (person.first_name and person.last_name):
            # Not a miss worth charging for - we never made the call.
            return None, ProviderCost(units=0, unit_cost_usd=0.0, hit=False)

        started = time.perf_counter()
        params = {"domain": domain, "first_name": person.first_name,
                  "last_name": person.last_name, "api_key": key}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{BASE}/email-finder", params=params)
        latency = int((time.perf_counter() - started) * 1000)

        if resp.status_code >= 400:
            return None, ProviderCost(1, 0.0, hit=False, latency_ms=latency)

        data = (resp.json() or {}).get("data") or {}
        email, score = data.get("email"), data.get("score") or 0
        if not email or score < MIN_CONFIDENCE:
            return None, ProviderCost(1, self.est_unit_cost_usd, hit=False, latency_ms=latency)
        return email, ProviderCost(1, self.est_unit_cost_usd, hit=True, latency_ms=latency)

    async def test_key(self, key: str) -> bool:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{BASE}/account", params={"api_key": key})
        return r.status_code < 400


@register("verifier")
class HunterVerifier:
    name = "hunter"
    est_unit_cost_usd = 0.005

    _VERDICT = {"deliverable": "valid", "undeliverable": "invalid",
                "risky": "risky", "unknown": "unknown"}

    async def verify(self, email: str, *, key: str | None = None) -> VerifyResult:
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{BASE}/email-verifier",
                                    params={"email": email, "api_key": key})
        latency = int((time.perf_counter() - started) * 1000)

        if resp.status_code >= 400:
            return VerifyResult(email=email, verdict="unknown",
                                cost=ProviderCost(1, 0.0, hit=False, latency_ms=latency))

        d = (resp.json() or {}).get("data") or {}
        verdict = self._VERDICT.get(d.get("status", ""), "unknown")
        # A catch-all domain accepts everything, so "deliverable" there proves nothing.
        # Downgrade it rather than letting it into a cold campaign as valid.
        if d.get("accept_all"):
            verdict = "catch_all"
        return VerifyResult(
            email=email,
            verdict=verdict,
            mx_found=bool(d.get("mx_records")),
            smtp_accepts=d.get("smtp_check"),
            is_disposable=d.get("disposable"),
            is_role_account=d.get("webmail") is False and d.get("gibberish") is False and bool(d.get("role")),
            cost=ProviderCost(1, self.est_unit_cost_usd, hit=verdict != "unknown", latency_ms=latency),
            raw=d,
        )
