"""GitHub activity as a buying signal.

Useful because it is unambiguous and free: a company shipping releases weekly has an
engineering team with budget and momentum; one whose repos went quiet eight months ago
does not. No API key required for public data, though an unauthenticated caller gets
60 requests/hour, so results are cached by the caller.

Every signal states what was observed and links to the page it came from, because
"engineering activity: 87" is exactly the kind of unauditable score this project exists
to avoid.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .base import SignalRecord, score_signal

API = "https://api.github.com"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


async def org_signals(org: str, *, token: str | None = None,
                      now: datetime | None = None) -> list[SignalRecord]:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=30, headers=headers) as c:
            repos_r = await c.get(f"{API}/orgs/{org}/repos",
                                  params={"sort": "pushed", "per_page": 20})
            if repos_r.status_code == 404:
                return []
            if repos_r.status_code >= 400:
                return []
            repos = repos_r.json() or []
    except httpx.HTTPError:
        return []

    if not isinstance(repos, list) or not repos:
        return []

    out: list[SignalRecord] = []
    now = now or datetime.now(timezone.utc)

    active = [r for r in repos if _parse(r.get("pushed_at"))]
    if active:
        newest = max(active, key=lambda r: _parse(r["pushed_at"]))
        pushed = _parse(newest["pushed_at"])
        rec = SignalRecord(
            kind="github",
            summary=(f"{org} last pushed to {newest.get('name')} on "
                     f"{pushed.date().isoformat()}"),
            observed_at=pushed, source_url=newest.get("html_url"),
            raw={"repo": newest.get("name"), "stars": newest.get("stargazers_count")})
        rec.score = score_signal(0.5, rec, half_life_days=21, now=now)
        out.append(rec)

        recent = [r for r in active
                  if (now - _parse(r["pushed_at"]).astimezone(timezone.utc)).days <= 90]
        if len(recent) >= 3:
            rec = SignalRecord(
                kind="github",
                summary=(f"{org} pushed to {len(recent)} repositories in the last "
                         f"90 days"),
                observed_at=pushed,
                source_url=f"https://github.com/orgs/{org}/repositories",
                raw={"active_repos_90d": len(recent)})
            rec.score = score_signal(0.7, rec, half_life_days=21, now=now)
            out.append(rec)

    top = max(repos, key=lambda r: r.get("stargazers_count") or 0)
    if (top.get("stargazers_count") or 0) >= 500:
        pushed = _parse(top.get("pushed_at")) or now
        rec = SignalRecord(
            kind="github",
            summary=(f"{org} maintains {top.get('name')} with "
                     f"{top['stargazers_count']:,} stars"),
            observed_at=pushed, source_url=top.get("html_url"),
            raw={"stars": top.get("stargazers_count")})
        rec.score = score_signal(0.4, rec, half_life_days=180, now=now)
        out.append(rec)

    return out
