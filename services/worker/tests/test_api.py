import asyncio

import pytest
from fastapi.testclient import TestClient

from coldstack.api.main import app, store

client = TestClient(app)
DEMO = {"query": {"limit": 40}, "demo": True}


def _await_job(job_id, tries=60):
    for _ in range(tries):
        j = client.get(f"/jobs/{job_id}").json()
        if j["status"] in ("done", "failed", "cancelled"):
            return j
        import time; time.sleep(0.05)
    raise AssertionError("job never finished")


def test_health():
    assert client.get("/health").json()["ok"] is True


def test_transports_marks_eligibility_per_campaign_class():
    cold = {t["name"]: t for t in client.get("/transports?campaign_class=cold").json()["transports"]}
    assert cold["smtp"]["eligible"] is True
    assert cold["resend"]["eligible"] is False
    opt_in = {t["name"]: t for t in client.get("/transports?campaign_class=opt_in").json()["transports"]}
    assert opt_in["resend"]["eligible"] is True


def test_credentials_roundtrip_never_returns_the_secret():
    r = client.put("/credentials/apollo", json={"creds": {"api_key": "sk_test_WXYZ9876"}})
    assert r.status_code == 200
    body = r.json()
    assert body["hints"] == {"api_key": "...9876"}
    assert "sk_test_WXYZ9876" not in r.text

    listed = client.get("/credentials").json()["credentials"]
    assert "sk_test_WXYZ9876" not in str(listed)
    assert client.delete("/credentials/apollo").status_code == 200


def test_credentials_are_isolated_per_workspace():
    client.put("/credentials/hunter", json={"creds": {"api_key": "key-alpha"}},
               headers={"X-Workspace-Id": "alpha"})
    other = client.get("/credentials", headers={"X-Workspace-Id": "beta"}).json()
    assert other["credentials"] == []
    assert store.get("beta", "hunter") is None
    assert store.get("alpha", "hunter") == {"api_key": "key-alpha"}


def test_empty_credentials_rejected():
    assert client.put("/credentials/apollo", json={"creds": {"api_key": ""}}).status_code == 400


def test_build_without_keys_is_refused_with_a_useful_message():
    r = client.post("/lists/build", json={"query": {"limit": 5}, "demo": False},
                    headers={"X-Workspace-Id": "empty-ws"})
    assert r.status_code == 400
    assert "demo=true" in r.json()["detail"]


def test_preview_costs_nothing_and_says_so():
    r = client.post("/search/preview", json=DEMO).json()
    assert len(r["sample"]) > 0
    assert all(k not in r["sample"][0] for k in ("email",))     # search reveals no emails
    assert "no email addresses" in r["note"]


def test_build_job_then_export_csv():
    started = client.post("/lists/build", json=DEMO)
    assert started.status_code == 202
    job = _await_job(started.json()["id"])
    assert job["status"] == "done", job.get("error")
    assert job["report"]["emails_found"] > 0

    leads = client.get(f"/jobs/{job['id']}/leads?only_valid=true").json()
    assert leads["total"] > 0
    assert all(l["email_status"] == "valid" for l in leads["leads"])

    csv_resp = client.get(f"/jobs/{job['id']}/export.csv")
    assert csv_resp.status_code == 200
    assert "attachment" in csv_resp.headers["content-disposition"]
    header, *rows = csv_resp.text.strip().splitlines()
    assert header.startswith("first_name,last_name")
    assert len(rows) == leads["total"]


def test_export_before_completion_is_409_not_an_empty_file():
    """An empty CSV would look like a finished run with no results."""
    job_id = client.post("/lists/build", json={"query": {"limit": 300}, "demo": True}).json()["id"]
    r = client.get(f"/jobs/{job_id}/export.csv")
    assert r.status_code in (409, 200)
    if r.status_code == 409:
        assert "running" in r.json()["detail"] or "queued" in r.json()["detail"]


def test_unknown_job_is_404():
    assert client.get("/jobs/deadbeef").status_code == 404
    assert client.post("/jobs/deadbeef/cancel").status_code == 409


def test_budget_is_reported_back_to_the_client():
    r = client.post("/lists/build",
                    json={"query": {"limit": 200}, "demo": True, "budget_usd": 0.05})
    job = _await_job(r.json()["id"])
    assert job["report"]["budget_exhausted"] is True
    assert job["report"]["total_cost_usd"] <= 0.10
