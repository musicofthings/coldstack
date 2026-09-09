"""DNS doctor tests use a stub resolver: deterministic, and they can assert on records
that would be impractical to find in the wild (an 11-lookup SPF chain, +all, a short
DKIM key)."""
import dns.exception
import dns.resolver
import pytest

from coldstack.sending import dns_doctor as dd
from coldstack.sending.dns_doctor import Severity


class FakeRdata:
    def __init__(self, text: str):
        # Mimic the >255-byte split that real resolvers return.
        self.strings = [text[i:i + 255].encode() for i in range(0, len(text), 255)] or [b""]


class FakeMX:
    def __init__(self, pref, host):
        self.preference, self.exchange = pref, host


class FakeResolver:
    """zone: name -> list[str] | Exception."""
    def __init__(self, zone, mx=("mail.example.com",)):
        self.zone, self.mx = zone, mx

    def resolve(self, name, rdtype, tcp=False):
        if rdtype == "MX":
            if not self.mx:
                raise dns.resolver.NoAnswer()
            return [FakeMX(10, h) for h in self.mx]
        val = self.zone.get(name)
        if val is None:
            raise dns.resolver.NXDOMAIN()
        if isinstance(val, Exception):
            raise val
        return [FakeRdata(v) for v in val]


def sev(findings, check):
    return next(f.severity for f in findings if f.check == check)


# ---------------------------------------------------------------- regression

def test_a_timeout_is_never_reported_as_a_missing_record():
    """The original bug: a flaky resolver produced 'no SPF record, NOT SAFE TO SEND',
    which would send a user editing DNS that was never broken."""
    res = FakeResolver({"example.com": dns.exception.Timeout()})
    findings = dd.check_spf("example.com", res)
    assert sev(findings, "SPF") is Severity.INFO
    assert "unknown" in findings[0].detail
    assert findings[0].severity is not Severity.FAIL


def test_inconclusive_run_is_not_a_green_light():
    report = dd.DomainReport("example.com", [
        dd.Finding("SPF", Severity.INFO, "lookup failed (Timeout) - unknown")])
    assert report.inconclusive is True
    assert report.can_send is False


def test_authoritative_absence_is_still_a_failure():
    """NXDOMAIN really does mean the record is not there."""
    res = FakeResolver({})
    assert sev(dd.check_spf("example.com", res), "SPF") is Severity.FAIL


# ---------------------------------------------------------------- SPF

def test_spf_lookup_limit_is_counted_through_includes():
    zone = {
        "example.com": ["v=spf1 include:a.com include:b.com -all"],
        "a.com": ["v=spf1 include:c.com include:d.com a mx -all"],
        "b.com": ["v=spf1 a mx ptr -all"],
        "c.com": ["v=spf1 a mx -all"],
        "d.com": ["v=spf1 a -all"],
    }
    # 2 includes + (2 includes + a + mx) + (a+mx+ptr) + (a+mx) + a = 12 > 10
    findings = dd.check_spf("example.com", FakeResolver(zone))
    assert sev(findings, "SPF lookups") is Severity.FAIL
    assert "permerror" in next(f.fix for f in findings if f.check == "SPF lookups")


def test_spf_within_budget_passes():
    zone = {"example.com": ["v=spf1 include:_spf.google.com -all"],
            "_spf.google.com": ["v=spf1 ip4:1.2.3.4 -all"]}
    findings = dd.check_spf("example.com", FakeResolver(zone))
    assert sev(findings, "SPF lookups") is Severity.OK
    assert sev(findings, "SPF policy") is Severity.OK


def test_two_spf_records_is_a_permerror():
    res = FakeResolver({"example.com": ["v=spf1 a -all", "v=spf1 mx -all"]})
    assert sev(dd.check_spf("example.com", res), "SPF") is Severity.FAIL


def test_plus_all_is_fatal():
    res = FakeResolver({"example.com": ["v=spf1 +all"]})
    assert sev(dd.check_spf("example.com", res), "SPF policy") is Severity.FAIL


def test_softfail_is_allowed_but_noted():
    res = FakeResolver({"example.com": ["v=spf1 a ~all"]})
    assert sev(dd.check_spf("example.com", res), "SPF policy") is Severity.INFO


def test_long_txt_record_is_reassembled():
    """A >255 char record arrives in chunks; joining with a separator corrupts it."""
    long_spf = "v=spf1 " + " ".join(f"ip4:10.0.{i}.0/24" for i in range(30)) + " -all"
    assert len(long_spf) > 255
    res = FakeResolver({"example.com": [long_spf]})
    assert sev(dd.check_spf("example.com", res), "SPF policy") is Severity.OK


# ---------------------------------------------------------------- DKIM / DMARC

def test_dkim_found_on_a_common_selector():
    res = FakeResolver({"google._domainkey.example.com": ["v=DKIM1; k=rsa; p=" + "A" * 400]})
    assert dd.check_dkim("example.com", res).severity is Severity.OK


def test_dkim_missing_is_a_failure():
    assert dd.check_dkim("example.com", FakeResolver({})).severity is Severity.FAIL


def test_dkim_all_lookups_failing_is_unknown_not_missing():
    zone = {f"{s}._domainkey.example.com": dns.exception.Timeout()
            for s in dd.COMMON_DKIM_SELECTORS}
    f = dd.check_dkim("example.com", FakeResolver(zone))
    assert f.severity is Severity.INFO and "unknown" in f.detail


def test_short_dkim_key_warns():
    res = FakeResolver({"google._domainkey.example.com": ["v=DKIM1; p=" + "A" * 100]})
    assert dd.check_dkim("example.com", res).severity is Severity.WARN


def test_dmarc_without_rua_warns():
    res = FakeResolver({"_dmarc.example.com": ["v=DMARC1; p=quarantine"]})
    assert sev(dd.check_dmarc("example.com", res), "DMARC rua") is Severity.WARN


def test_dmarc_missing_fails():
    assert sev(dd.check_dmarc("example.com", FakeResolver({})), "DMARC") is Severity.FAIL


# ---------------------------------------------------------------- misc

def test_missing_mx_fails():
    assert dd.check_mx("example.com", FakeResolver({}, mx=())).severity is Severity.FAIL


@pytest.mark.parametrize("tracking,expected", [
    ("link.example.com", Severity.WARN),      # same root domain
    ("track.otherdomain.io", Severity.OK),
    (None, Severity.INFO),
])
def test_tracking_domain_must_be_separate(tracking, expected):
    assert dd.check_tracking_domain("example.com", tracking).severity is expected
