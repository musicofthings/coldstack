"""Reacher (check-if-email-exists) - self-hosted SMTP verification.

The reason this is worth running yourself: zero marginal cost. Every commercial
verifier charges per address, and on a 50k list that is real money for a check that
is fundamentally just an SMTP conversation.

Operational reality, documented here because it bites everyone once:
Reacher needs outbound port 25, which AWS, GCP, Azure and most PaaS block outright.
Run it on a provider that permits it (Hetzner, OVH, a bare VPS), with a clean IP and
a correct PTR record, or every verdict comes back unknown and you will think the
software is broken.
"""
from __future__ import annotations

import time

import httpx

from ..providers.base import ProviderCost, VerifyResult
from ..providers.registry import register

# Reacher's reachability verdicts -> ours.
_VERDICT = {"safe": "valid", "invalid": "invalid", "risky": "risky", "unknown": "unknown"}


@register("verifier")
class ReacherVerifier:
    name = "reacher"
    est_unit_cost_usd = 0.0            # self-hosted: the whole point

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or "http://localhost:8080").rstrip("/")

    async def verify(self, email: str, *, key: str | None = None) -> VerifyResult:
        started = time.perf_counter()
        headers = {"Authorization": key} if key else {}
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(f"{self.base_url}/v1/check_email",
                                         headers=headers, json={"to_email": email})
        except httpx.HTTPError as exc:
            return VerifyResult(email=email, verdict="unknown",
                                cost=ProviderCost(1, 0.0, hit=False),
                                raw={"error": str(exc)})

        latency = int((time.perf_counter() - started) * 1000)
        if resp.status_code >= 400:
            return VerifyResult(email=email, verdict="unknown",
                                cost=ProviderCost(1, 0.0, hit=False, latency_ms=latency),
                                raw={"status": resp.status_code, "body": resp.text[:300]})

        d = resp.json() or {}
        smtp = d.get("smtp") or {}
        misc = d.get("misc") or {}
        mx = d.get("mx") or {}
        verdict = _VERDICT.get(d.get("is_reachable", "unknown"), "unknown")

        # Reacher reports catch-all inside smtp rather than as a top-level verdict.
        # A catch-all accepts everything, so "safe" there is not evidence of anything.
        if smtp.get("is_catch_all"):
            verdict = "catch_all"

        return VerifyResult(
            email=email,
            verdict=verdict,
            mx_found=bool(mx.get("accepts_mail")),
            smtp_accepts=smtp.get("is_deliverable"),
            is_disposable=misc.get("is_disposable"),
            is_role_account=misc.get("is_role_account"),
            cost=ProviderCost(1, 0.0, hit=verdict != "unknown", latency_ms=latency),
            raw=d,
        )
