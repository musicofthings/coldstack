"""Gmail transport tests use a stub token minter and a stub HTTP layer, so nothing
touches Google and no credential is needed to run the suite."""
import base64
import email

import pytest

from coldstack.sending.base import SendRequest
from coldstack.sending.policy import CampaignClass, assert_allowed
from coldstack.sending.registry import all_transports
from coldstack.sending.transports.gmail import (DelegatedTokens, GmailTransport,
                                                TokenError, _explain, build_mime)


def req(**kw):
    base = dict(from_email="me@oncophenomics.com", from_name="Shibi",
                to_email="ana@target.com", to_name="Ana Cruz",
                subject="quick question", text="Worth a chat?")
    return SendRequest(**{**base, **kw})


def mime_of(r):
    return email.message_from_bytes(base64.urlsafe_b64decode(build_mime(r)))


# ---------------------------------------------------------------- MIME

def test_mime_carries_addresses_and_subject():
    m = mime_of(req())
    assert m["Subject"] == "quick question"
    assert "ana@target.com" in m["To"] and "Shibi" in m["From"]


def test_threading_headers_are_written_when_present():
    m = mime_of(req(in_reply_to="<m1@x>", references=["<m1@x>", "<m2@x>"]))
    assert m["In-Reply-To"] == "<m1@x>"
    assert m["References"] == "<m1@x> <m2@x>"


def test_custom_headers_survive():
    m = mime_of(req(headers={"List-Unsubscribe": "<mailto:u@x.com>"}))
    assert m["List-Unsubscribe"] == "<mailto:u@x.com>"


def test_raw_is_url_safe_base64():
    raw = build_mime(req(text="a+b/c" * 400))
    assert "+" not in raw and "/" not in raw          # would corrupt the API payload


# ---------------------------------------------------------------- error translation

def test_unauthorized_client_is_explained_as_a_missing_delegation_grant():
    msg = _explain("unauthorized_client: Client is unauthorized", "a@b.com")
    assert "Domain-wide Delegation" in msg
    assert "gmail.send" in msg


def test_invalid_grant_is_explained_as_a_missing_mailbox():
    msg = _explain("invalid_grant: Invalid email or User ID", "ghost@oncophenomics.com")
    assert "not a mailbox on the domain" in msg
    assert "ghost@oncophenomics.com" in msg


def test_the_two_delegation_errors_do_not_produce_the_same_advice():
    """They mean opposite things: one is a console setting, the other is a typo."""
    a = _explain("unauthorized_client", "x@y.com")
    b = _explain("invalid_grant", "x@y.com")
    assert a != b


# ---------------------------------------------------------------- token cache

class StubMinter(DelegatedTokens):
    def __init__(self):
        super().__init__(service_account_info={})
        self.calls = 0

    def token(self, subject):
        self.calls += 1
        return f"tok-{subject}-{self.calls}"


def test_token_is_cached_until_close_to_expiry(monkeypatch):
    import time as _t
    d = DelegatedTokens(service_account_info={})
    d._cache["a@b.com"] = ("cached", _t.time() + 3600)
    assert d.token("a@b.com") == "cached"


def test_a_nearly_expired_token_is_not_reused():
    import time as _t
    d = DelegatedTokens(service_account_info={})
    d._cache["a@b.com"] = ("stale", _t.time() + 30)      # inside the 120s skew
    with pytest.raises(TokenError):
        d.token("a@b.com")                               # would try to mint, no creds


def test_forget_drops_the_entry():
    import time as _t
    d = DelegatedTokens(service_account_info={})
    d._cache["a@b.com"] = ("x", _t.time() + 3600)
    d.forget("a@b.com")
    assert "a@b.com" not in d._cache


# ---------------------------------------------------------------- send path

class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload or {}, text

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, post_responses, get_response=None):
        self.post_responses = list(post_responses)
        self.get_response = get_response
        self.posts, self.gets = [], []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.posts.append((url, headers, json))
        return self.post_responses.pop(0)

    async def get(self, url, headers=None, params=None):
        self.gets.append((url, params))
        return self.get_response or FakeResponse(404)


def patch_client(monkeypatch, client):
    import coldstack.sending.transports.gmail as g
    monkeypatch.setattr(g.httpx, "AsyncClient", lambda **kw: client)


async def test_successful_send_returns_ids(monkeypatch):
    sent = FakeResponse(200, {"id": "gmid1", "threadId": "thr1"})
    meta = FakeResponse(200, {"payload": {"headers": [
        {"name": "Message-ID", "value": "<real@mail.gmail.com>"}]}})
    client = FakeClient([sent], meta)
    patch_client(monkeypatch, client)

    t = GmailTransport(tokens=StubMinter())
    r = await t.send(req(), creds={"subject": "me@oncophenomics.com"})
    assert r.ok
    assert r.provider_message_id == "gmid1"
    assert r.thread_id == "thr1"
    assert r.message_id_hdr == "<real@mail.gmail.com>"


async def test_the_real_message_id_is_read_back(monkeypatch):
    """Gmail replaces any Message-ID we set. Without reading it back, an inbound reply
    cannot be matched to its enrollment by header chain."""
    client = FakeClient([FakeResponse(200, {"id": "m", "threadId": "t"})],
                        FakeResponse(200, {"payload": {"headers": []}}))
    patch_client(monkeypatch, client)
    t = GmailTransport(tokens=StubMinter())
    r = await t.send(req(), creds={"subject": "me@oncophenomics.com"})
    assert r.ok and r.message_id_hdr is None      # absent header, not a crash
    assert client.gets and client.gets[0][1]["metadataHeaders"] == "Message-ID"


async def test_follow_up_is_threaded_by_gmail_thread_id(monkeypatch):
    client = FakeClient([FakeResponse(200, {"id": "m2", "threadId": "thr1"})],
                        FakeResponse(200, {"payload": {"headers": []}}))
    patch_client(monkeypatch, client)
    t = GmailTransport(tokens=StubMinter())
    await t.send(req(in_reply_to="<m1@x>"),
                 creds={"subject": "me@oncophenomics.com", "thread_id": "thr1"})
    assert client.posts[0][2]["threadId"] == "thr1"


async def test_first_touch_sends_no_thread_id(monkeypatch):
    client = FakeClient([FakeResponse(200, {"id": "m", "threadId": "t"})],
                        FakeResponse(200, {"payload": {"headers": []}}))
    patch_client(monkeypatch, client)
    t = GmailTransport(tokens=StubMinter())
    await t.send(req(), creds={"subject": "me@oncophenomics.com"})
    assert "threadId" not in client.posts[0][2]


async def test_a_401_refreshes_the_token_and_retries_once(monkeypatch):
    client = FakeClient([FakeResponse(401, text="expired"),
                         FakeResponse(200, {"id": "m", "threadId": "t"})],
                        FakeResponse(200, {"payload": {"headers": []}}))
    patch_client(monkeypatch, client)
    minter = StubMinter()
    t = GmailTransport(tokens=minter)
    r = await t.send(req(), creds={"subject": "me@oncophenomics.com"})
    assert r.ok
    assert len(client.posts) == 2
    assert client.posts[0][1] != client.posts[1][1]      # a fresh token was used


@pytest.mark.parametrize("status,retryable", [
    (429, True), (500, True), (503, True), (403, False), (400, False),
])
async def test_retryability_matches_the_failure_kind(monkeypatch, status, retryable):
    """403 is usually quota or policy; repeating it makes things worse."""
    patch_client(monkeypatch, FakeClient([FakeResponse(status, text="err")]))
    t = GmailTransport(tokens=StubMinter())
    r = await t.send(req(), creds={"subject": "me@oncophenomics.com"})
    assert r.ok is False and r.retryable is retryable


async def test_a_delegation_problem_is_not_retryable(monkeypatch):
    class Broken(StubMinter):
        def token(self, subject):
            raise TokenError("delegation not authorised for these scopes")
    t = GmailTransport(tokens=Broken())
    r = await t.send(req(), creds={"subject": "me@oncophenomics.com"})
    assert r.ok is False and r.retryable is False
    assert "delegation" in r.error


# ---------------------------------------------------------------- registry + policy

def test_gmail_is_registered_and_cold_safe():
    t = all_transports()["gmail"]
    assert t.caps.cold_outreach_safe is True
    assert_allowed(CampaignClass.COLD, t)          # must not raise


def test_gmail_daily_cap_is_reputational_not_the_api_limit():
    """Workspace permits 2,000/day. That is not the number that matters."""
    assert all_transports()["gmail"].caps.suggested_daily_cap <= 50
