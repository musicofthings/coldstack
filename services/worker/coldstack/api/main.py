"""ColdStack API.

    uvicorn coldstack.api.main:app --reload --port 8000

Single-tenant by default: the workspace comes from the X-Workspace-Id header and
falls back to "default". Real auth arrives with the Next.js app in P1 - this is a
seam, not a security model, and the app refuses to start on 0.0.0.0 without one.
"""
from __future__ import annotations

import io
import os
from dataclasses import asdict
from typing import Any, Literal

from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..pipeline.build_list import Lead, build_list
from ..pipeline.export import COLUMNS
from ..providers import apollo, fake, hunter          # noqa: F401 - registers adapters
from ..providers.base import SearchQuery
from ..providers.registry import email_finders, search_providers, verifiers
from ..sending.policy import CampaignClass, eligible
from ..sending.registry import all_transports, describe
from ..vault import Vault, generate_master_key
from .jobs import JobRegistry
from .store import InMemoryStore

MASTER_KEY = os.getenv("CREDENTIAL_MASTER_KEY") or generate_master_key()
if not os.getenv("CREDENTIAL_MASTER_KEY"):
    # Ephemeral key: credentials survive only while this process does. Loud on purpose -
    # silently generating one and pretending it persists is how people lose their keys.
    print("WARNING: CREDENTIAL_MASTER_KEY not set - using an ephemeral key. "
          "Credentials will not survive a restart. Generate one with "
          "`python -m coldstack.cli genkey`.")

store = InMemoryStore(vault=Vault(MASTER_KEY))
jobs = JobRegistry()
app = FastAPI(title="ColdStack", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["*"], allow_headers=["*"],
)


def ws(x_workspace_id: str | None = Header(default=None)) -> str:
    return x_workspace_id or "default"


# ------------------------------------------------------------------ models

class QueryIn(BaseModel):
    titles: list[str] = []
    seniorities: list[str] = []
    departments: list[str] = []
    industries: list[str] = []
    countries: list[str] = []
    tech_any: list[str] = []
    keywords: list[str] = []
    headcount_min: int | None = None
    headcount_max: int | None = None
    limit: int = Field(default=100, ge=1, le=50_000)

    def to_query(self) -> SearchQuery:
        return SearchQuery(**self.model_dump())


class BuildIn(BaseModel):
    query: QueryIn
    demo: bool = False
    verify: bool = True
    budget_usd: float | None = Field(default=None, gt=0)
    concurrency: int = Field(default=8, ge=1, le=32)
    suppressed_domains: list[str] = []
    suppressed_emails: list[str] = []


class CredentialsIn(BaseModel):
    creds: dict[str, str]


# ------------------------------------------------------------------ meta

@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": app.version,
            "credential_key": "configured" if os.getenv("CREDENTIAL_MASTER_KEY") else "ephemeral"}


@app.get("/providers")
def providers() -> dict[str, Any]:
    return {
        "search": sorted(search_providers()),
        "finders": [{"name": n, "est_unit_cost_usd": getattr(c, "est_unit_cost_usd", None)}
                    for n, c in sorted(email_finders().items())],
        "verifiers": [{"name": n, "est_unit_cost_usd": getattr(c, "est_unit_cost_usd", None)}
                      for n, c in sorted(verifiers().items())],
    }


@app.get("/transports")
def transports(campaign_class: Literal["cold", "warm", "opt_in", "transactional"] | None = None) -> dict[str, Any]:
    rows = describe()
    if campaign_class:
        allowed = set(eligible(CampaignClass(campaign_class), all_transports()))
        rows = [r | {"eligible": r["name"] in allowed} for r in rows]
    return {"transports": rows}


# ------------------------------------------------------------------ credentials

@app.get("/credentials")
def list_credentials(workspace: str = Header(default="default", alias="X-Workspace-Id")):
    return {"credentials": [asdict(c) for c in store.list_credentials(workspace)]}


@app.put("/credentials/{provider}")
def put_credentials(provider: str, body: CredentialsIn,
                    workspace: str = Header(default="default", alias="X-Workspace-Id")):
    try:
        info = store.put(workspace, provider, body.creds)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return asdict(info)           # hints only - never the values


@app.post("/credentials/{provider}/test")
async def test_credentials(provider: str,
                           workspace: str = Header(default="default", alias="X-Workspace-Id")):
    creds = store.get(workspace, provider)
    if not creds:
        raise HTTPException(404, f"no credentials stored for {provider}")
    adapter = (search_providers().get(provider) or email_finders().get(provider))
    if not adapter:
        raise HTTPException(404, f"no adapter named {provider}")
    key = creds.get("api_key") or next(iter(creds.values()))
    try:
        ok = await adapter().test_key(key)
    except Exception as exc:                                   # noqa: BLE001
        store.set_status(workspace, provider, "failing", str(exc))
        return {"provider": provider, "ok": False, "error": str(exc)}
    store.set_status(workspace, provider, "ok" if ok else "failing",
                     None if ok else "credentials rejected")
    return {"provider": provider, "ok": ok}


@app.delete("/credentials/{provider}")
def delete_credentials(provider: str,
                       workspace: str = Header(default="default", alias="X-Workspace-Id")):
    if not store.delete(workspace, provider):
        raise HTTPException(404, f"no credentials stored for {provider}")
    return {"deleted": provider}


# ------------------------------------------------------------------ build

def _resolve(workspace: str, demo: bool, verify: bool) -> dict[str, Any]:
    """Pick adapters from what this workspace actually has keys for."""
    if demo:
        return dict(search=search_providers()["fake"](), search_key="",
                    finders={"fake": email_finders()["fake"]()}, finder_keys={"fake": "x"},
                    verifier=verifiers()["fake"]() if verify else None, verifier_key=None)

    apollo_creds = store.get(workspace, "apollo")
    if not apollo_creds:
        raise HTTPException(400, "no Apollo credentials stored - PUT /credentials/apollo, "
                                 "or send demo=true to use the fake providers")
    apollo_key = apollo_creds["api_key"]
    hunter_creds = store.get(workspace, "hunter")
    hunter_key = hunter_creds["api_key"] if hunter_creds else None

    finders: dict[str, Any] = {}
    finder_keys: dict[str, str] = {}
    if hunter_key:
        finders["hunter"] = email_finders()["hunter"]()
        finder_keys["hunter"] = hunter_key
    finders["apollo_enrich"] = email_finders()["apollo_enrich"]()
    finder_keys["apollo_enrich"] = apollo_key

    return dict(search=search_providers()["apollo"](), search_key=apollo_key,
                finders=finders, finder_keys=finder_keys,
                verifier=verifiers()["hunter"]() if (verify and hunter_key) else None,
                verifier_key=hunter_key)


@app.post("/search/preview")
async def preview(body: BuildIn,
                  workspace: str = Header(default="default", alias="X-Workspace-Id"),
                  sample: int = Query(default=25, ge=1, le=100)):
    """Search only - no enrichment, so no spend. What the filter screen calls on every
    change, so the user sees who they are about to pay to enrich."""
    kit = _resolve(workspace, body.demo, verify=False)
    q = body.query.to_query()
    q.limit = min(q.limit, sample)
    result = await kit["search"].search(q, key=kit["search_key"])
    est = sum(f.est_unit_cost_usd for f in kit["finders"].values()) / max(len(kit["finders"]), 1)
    return {
        "sample": [{"full_name": p.full_name, "title": p.title,
                    "company": p.company.name if p.company else None,
                    "domain": p.company.domain if p.company else None,
                    "headcount": p.company.headcount if p.company else None,
                    "country": p.company.country if p.company else None,
                    "linkedin_url": p.linkedin_url} for p in result.people],
        "total_available": result.total_available,
        "unsupported_filters": result.unsupported_filters,
        "estimated_enrichment_usd": round(est * (body.query.limit or 0), 2),
        "note": "search returns no email addresses; enrichment is a separate paid step",
    }


@app.post("/lists/build", status_code=202)
async def start_build(body: BuildIn,
                      workspace: str = Header(default="default", alias="X-Workspace-Id")):
    kit = _resolve(workspace, body.demo, body.verify)

    async def _run():
        leads, report = await build_list(
            body.query.to_query(), **kit,
            suppressed_emails=set(body.suppressed_emails),
            suppressed_domains=set(body.suppressed_domains),
            budget_usd=body.budget_usd, concurrency=body.concurrency)
        return {"leads": leads, "report": report}

    job = jobs.start("build_list", _run)
    return job.public()


@app.get("/jobs")
def list_jobs():
    return {"jobs": [j.public() for j in jobs.list_jobs()]}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    out = job.public()
    if job.status == "done":
        rep = job.result["report"]
        out["report"] = asdict(rep)
        out["report"]["total_cost_usd"] = round(rep.total_cost_usd, 5)
        out["report"]["cost_per_valid_email"] = round(rep.cost_per_valid_email, 5)
        for prov in out["report"]["per_provider"].values():
            prov["cost"] = round(prov["cost"], 5)
        out["count"] = len(job.result["leads"])
    return out


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    if not jobs.cancel(job_id):
        raise HTTPException(409, "job is not cancellable (already finished or unknown)")
    return {"cancelled": job_id}


def _finished(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if job.status != "done":
        raise HTTPException(409, f"job is {job.status}")
    return job


@app.get("/jobs/{job_id}/leads")
def job_leads(job_id: str, only_valid: bool = False, include_catch_all: bool = False,
              offset: int = 0, limit: int = Query(default=100, ge=1, le=1000)):
    leads = _filter(_finished(job_id).result["leads"], only_valid, include_catch_all)
    return {"total": len(leads),
            "leads": [asdict(l) for l in leads[offset:offset + limit]]}


def _filter(leads: list[Lead], only_valid: bool, include_catch_all: bool) -> list[Lead]:
    out = [l for l in leads if l.email]
    if only_valid:
        keep = {"valid"} | ({"catch_all"} if include_catch_all else set())
        out = [l for l in out if l.email_status in keep]
    return out


@app.get("/jobs/{job_id}/export.csv")
def export_csv(job_id: str, only_valid: bool = True, include_catch_all: bool = False):
    import csv
    leads = _filter(_finished(job_id).result["leads"], only_valid, include_catch_all)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows({c: getattr(l, c) for c in COLUMNS} for l in leads)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="coldstack-{job_id}.csv"'})
