"""DNS doctor - authentication health for a sending domain.

Not a checkbox. Google and Microsoft tightened bulk-sender requirements through
2024-2026, and the failures that actually burn domains are the quiet ones: an SPF
record that exceeds its DNS-lookup budget and silently becomes a permerror, a DKIM key
short enough to be ignored, a DMARC record with no rua so nobody ever sees the reports,
a tracking domain sharing reputation with the sending domain.

Each check returns a Finding with a severity and a fix, because "SPF: FAIL" helps
nobody at 2am.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

import dns.exception
import dns.resolver

# RFC 7208 s4.6.4: an SPF evaluation may trigger at most 10 DNS-querying mechanisms.
# Exceed it and conformant receivers return permerror - which most treat as "no SPF".
SPF_LOOKUP_LIMIT = 10
SPF_LOOKUP_MECHANISMS = ("include:", "a:", "mx:", "ptr:", "exists:", "redirect=")

# Selectors worth probing before asking the user. Google Workspace uses "google";
# Microsoft uses selector1/selector2; the rest are common ESP defaults.
COMMON_DKIM_SELECTORS = ("google", "selector1", "selector2", "k1", "k2", "s1", "s2",
                         "default", "dkim", "mail", "smtp", "mandrill", "pm-bounces")

MIN_DKIM_BITS = 1024


class Severity(str, Enum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    FAIL = "fail"

    def __str__(self) -> str:
        return self.value


@dataclass(slots=True)
class Finding:
    check: str
    severity: Severity
    detail: str
    fix: str | None = None


@dataclass(slots=True)
class DomainReport:
    domain: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def inconclusive(self) -> bool:
        return any("unknown" in f.detail for f in self.findings)

    @property
    def can_send(self) -> bool:
        """Any FAIL means mail from this domain will be filtered or rejected.
        Unknown is not the same as fine, so an inconclusive run is not a green light."""
        return (not any(f.severity is Severity.FAIL for f in self.findings)
                and not self.inconclusive)

    def by_severity(self, sev: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity is sev]

    def summary(self) -> str:
        icon = {Severity.OK: "OK  ", Severity.INFO: "INFO", Severity.WARN: "WARN",
                Severity.FAIL: "FAIL"}
        state = ("ready to send" if self.can_send
                 else "INCOMPLETE - some checks could not run" if self.inconclusive
                 and not self.by_severity(Severity.FAIL) else "NOT SAFE TO SEND")
        lines = [f"{self.domain}  -  {state}"]
        for f in self.findings:
            lines.append(f"  [{icon[f.severity]}] {f.check}: {f.detail}")
            if f.fix and f.severity in (Severity.WARN, Severity.FAIL):
                lines.append(f"         fix: {f.fix}")
        return "\n".join(lines)


@dataclass(slots=True)
class TxtLookup:
    """A DNS answer and whether we actually got one.

    This distinction is the whole point: NXDOMAIN/NoAnswer is the resolver telling us
    authoritatively that no record exists, while a timeout tells us nothing at all.
    Collapsing the two - as the first version of this module did - reports a flaky
    resolver as "no SPF record, NOT SAFE TO SEND" and sends the user editing DNS that
    was never broken. An inconclusive check must never masquerade as a failed one.
    """
    records: list[str]
    resolved: bool          # False = we could not find out
    error: str | None = None


def _txt(name: str, resolver: dns.resolver.Resolver) -> TxtLookup:
    for use_tcp in (False, True):
        # TXT answers are frequently large enough to be truncated over UDP; retrying
        # over TCP recovers the common case before we give up.
        try:
            answers = resolver.resolve(name, "TXT", tcp=use_tcp)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return TxtLookup([], resolved=True)
        except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
            last = f"{type(exc).__name__}"
            continue
        out = []
        for rdata in answers:
            # A TXT record longer than 255 bytes arrives as multiple strings that must
            # be concatenated with no separator - a long SPF or DKIM record parses as
            # garbage otherwise.
            out.append("".join(x.decode() if isinstance(x, bytes) else x
                               for x in rdata.strings))
        return TxtLookup(out, resolved=True)
    return TxtLookup([], resolved=False, error=last)


def _resolver(timeout: float) -> dns.resolver.Resolver:
    r = dns.resolver.Resolver()
    r.timeout = timeout
    r.lifetime = timeout
    return r


# ------------------------------------------------------------------ checks

def check_mx(domain: str, res: dns.resolver.Resolver) -> Finding:
    try:
        hosts = sorted((r.preference, str(r.exchange).rstrip(".")) for r in res.resolve(domain, "MX"))
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.Timeout,
            dns.resolver.NoNameservers):
        return Finding("MX", Severity.FAIL, "no MX record",
                       "Add MX records. Without them you cannot receive replies or bounces, "
                       "and many receivers reject mail from domains that cannot receive it.")
    return Finding("MX", Severity.OK, f"{len(hosts)} host(s), primary {hosts[0][1]}")


def _count_spf_lookups(record: str, domain: str, res: dns.resolver.Resolver,
                       seen: set[str] | None = None, depth: int = 0) -> int:
    """Recursively count DNS-querying mechanisms, following includes and redirects."""
    seen = seen if seen is not None else set()
    if depth > 10:
        return SPF_LOOKUP_LIMIT + 1
    count = 0
    for term in record.split():
        low = term.lower().lstrip("+-~?")
        if not low.startswith(SPF_LOOKUP_MECHANISMS) and low not in ("a", "mx", "ptr"):
            continue
        count += 1
        target = None
        if low.startswith("include:"):
            target = term.split(":", 1)[1]
        elif low.startswith("redirect="):
            target = term.split("=", 1)[1]
        if target and target not in seen:
            seen.add(target)
            for nested in _txt(target, res).records:
                if nested.lower().startswith("v=spf1"):
                    count += _count_spf_lookups(nested, target, res, seen, depth + 1)
                    break
    return count


def check_spf(domain: str, res: dns.resolver.Resolver) -> list[Finding]:
    lookup = _txt(domain, res)
    if not lookup.resolved:
        return [Finding("SPF", Severity.INFO, f"lookup failed ({lookup.error}) - unknown",
                        "Could not read TXT records. This is not evidence that SPF is "
                        "missing; re-run from a host with a working resolver.")]
    records = [t for t in lookup.records if t.lower().startswith("v=spf1")]
    if not records:
        return [Finding("SPF", Severity.FAIL, "no v=spf1 record",
                        "Publish a TXT record at the domain apex, e.g. "
                        "'v=spf1 include:_spf.google.com -all'.")]
    if len(records) > 1:
        return [Finding("SPF", Severity.FAIL, f"{len(records)} SPF records published",
                        "Exactly one v=spf1 record is permitted. Multiple records are a "
                        "permerror and receivers treat the domain as having no SPF at all.")]

    record = records[0]
    out: list[Finding] = []
    lookups = _count_spf_lookups(record, domain, res)
    if lookups > SPF_LOOKUP_LIMIT:
        out.append(Finding("SPF lookups", Severity.FAIL,
                           f"{lookups} DNS lookups (limit {SPF_LOOKUP_LIMIT})",
                           "Over the limit is a permerror, so SPF stops working entirely "
                           "even though the record looks fine. Flatten includes or drop "
                           "unused ones."))
    elif lookups >= SPF_LOOKUP_LIMIT - 2:
        out.append(Finding("SPF lookups", Severity.WARN,
                           f"{lookups} DNS lookups (limit {SPF_LOOKUP_LIMIT})",
                           "Close to the limit - adding one more sender will break SPF."))
    else:
        out.append(Finding("SPF lookups", Severity.OK, f"{lookups} of {SPF_LOOKUP_LIMIT}"))

    tail = record.split()[-1].lower() if record.split() else ""
    if tail == "+all":
        out.append(Finding("SPF policy", Severity.FAIL, "'+all' permits the world to spoof you",
                           "Use '-all' (hard fail) or '~all' (soft fail)."))
    elif tail == "-all":
        out.append(Finding("SPF policy", Severity.OK, "-all (hard fail)"))
    elif tail == "~all":
        out.append(Finding("SPF policy", Severity.INFO, "~all (soft fail)",
                           "'-all' is stronger once you are confident every sender is listed."))
    else:
        out.append(Finding("SPF policy", Severity.WARN, f"no explicit all mechanism ({tail or 'none'})",
                           "End the record with '-all' or '~all'."))
    return out


def check_dkim(domain: str, res: dns.resolver.Resolver,
               selectors: Iterable[str] = COMMON_DKIM_SELECTORS) -> Finding:
    found: list[tuple[str, int]] = []
    inconclusive = 0
    selectors = tuple(selectors)
    for sel in selectors:
        lookup = _txt(f"{sel}._domainkey.{domain}", res)
        if not lookup.resolved:
            inconclusive += 1
            continue
        for txt in lookup.records:
            if "p=" not in txt:
                continue
            p = re.search(r"p=([A-Za-z0-9+/=]+)", txt)
            # ~1.34 base64 chars per byte of DER; the modulus is most of it.
            bits = int(len(p.group(1)) * 6 / 1.16) if p else 0
            found.append((sel, bits))
            break
    if not found and inconclusive:
        return Finding("DKIM", Severity.INFO,
                       f"{inconclusive}/{len(selectors)} selector lookups failed - unknown",
                       "Could not read DKIM records; this is not evidence that none exist.")
    if not found:
        return Finding("DKIM", Severity.FAIL,
                       f"no key found on {len(selectors)} common selectors",
                       "Publish a DKIM key and sign outbound mail. Unsigned mail fails "
                       "DMARC alignment and is the single biggest deliverability loss.")
    weak = [s for s, b in found if b and b < MIN_DKIM_BITS]
    sels = ", ".join(s for s, _ in found)
    if weak:
        return Finding("DKIM", Severity.WARN, f"selector(s) {sels}; {weak} look under "
                       f"{MIN_DKIM_BITS} bits",
                       "Rotate to a 2048-bit key. Short keys are ignored by some receivers.")
    return Finding("DKIM", Severity.OK, f"selector(s) {sels}")


def check_dmarc(domain: str, res: dns.resolver.Resolver) -> list[Finding]:
    lookup = _txt(f"_dmarc.{domain}", res)
    if not lookup.resolved:
        return [Finding("DMARC", Severity.INFO, f"lookup failed ({lookup.error}) - unknown",
                        "Could not read the _dmarc TXT record; re-run from a host with a "
                        "working resolver.")]
    records = [t for t in lookup.records if t.lower().startswith("v=dmarc1")]
    if not records:
        return [Finding("DMARC", Severity.FAIL, "no _dmarc record",
                        "Publish at least 'v=DMARC1; p=none; rua=mailto:you@domain'. "
                        "Bulk senders to Gmail and Outlook are required to have one.")]
    tags = dict(
        (k.strip().lower(), v.strip())
        for k, _, v in (part.partition("=") for part in records[0].split(";")) if k.strip()
    )
    out = [Finding("DMARC", Severity.OK, f"p={tags.get('p', 'none')}")]
    if tags.get("p") == "none":
        out.append(Finding("DMARC policy", Severity.INFO, "p=none only monitors",
                           "Move to p=quarantine once your reports are clean."))
    if not tags.get("rua"):
        out.append(Finding("DMARC rua", Severity.WARN, "no aggregate report address",
                           "Add rua=mailto:... - without it you are blind to alignment "
                           "failures until deliverability has already dropped."))
    return out


def check_tracking_domain(domain: str, tracking_domain: str | None) -> Finding:
    if not tracking_domain:
        return Finding("Tracking domain", Severity.INFO, "no tracking domain configured",
                       "Fine - open and click tracking are off by default in ColdStack.")
    root = ".".join(domain.split(".")[-2:])
    if tracking_domain.endswith(root):
        return Finding("Tracking domain", Severity.WARN,
                       f"{tracking_domain} shares the root domain with {domain}",
                       "Host tracking on a separate domain. A blocklisted tracking domain "
                       "otherwise drags your sending domain down with it.")
    return Finding("Tracking domain", Severity.OK, f"{tracking_domain} is separate")


def check_blocklist(domain: str, res: dns.resolver.Resolver) -> Finding:
    """Spamhaus DBL. Public resolvers are refused by Spamhaus, so an inconclusive answer
    is reported as such rather than as a clean bill of health."""
    query = f"{domain}.dbl.spamhaus.org"
    try:
        answers = [str(r) for r in res.resolve(query, "A")]
    except dns.resolver.NXDOMAIN:
        return Finding("Blocklist", Severity.OK, "not listed on Spamhaus DBL")
    except (dns.exception.Timeout, dns.resolver.NoNameservers, dns.resolver.NoAnswer):
        return Finding("Blocklist", Severity.INFO, "DBL lookup inconclusive",
                       "Spamhaus refuses queries from public resolvers - check from a "
                       "host with its own resolver for a real answer.")
    # 127.255.255.x is Spamhaus's "your query was blocked", not a listing.
    if any(a.startswith("127.255.255.") for a in answers):
        return Finding("Blocklist", Severity.INFO, "DBL refused the query (public resolver)",
                       "Query from a host with its own recursive resolver.")
    return Finding("Blocklist", Severity.FAIL, f"listed on Spamhaus DBL ({answers[0]})",
                   "Do not send. Resolve the listing at spamhaus.org first.")


def diagnose(domain: str, *, tracking_domain: str | None = None,
             dkim_selectors: Iterable[str] | None = None,
             timeout: float = 5.0, check_blocklists: bool = True) -> DomainReport:
    res = _resolver(timeout)
    report = DomainReport(domain=domain)
    report.findings.append(check_mx(domain, res))
    report.findings.extend(check_spf(domain, res))
    report.findings.append(check_dkim(domain, res, dkim_selectors or COMMON_DKIM_SELECTORS))
    report.findings.extend(check_dmarc(domain, res))
    report.findings.append(check_tracking_domain(domain, tracking_domain))
    if check_blocklists:
        report.findings.append(check_blocklist(domain, res))
    return report
