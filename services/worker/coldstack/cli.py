"""ColdStack CLI - the P0 slice, runnable without the web app.

    # no keys needed, uses the deterministic fake providers
    python -m coldstack.cli build --demo --limit 50 --out leads.csv

    # real run
    export APOLLO_API_KEY=... HUNTER_API_KEY=...
    python -m coldstack.cli build \
        --title "Head of Laboratory" --title "Director of Genomics" \
        --country "India" --headcount 20-500 --limit 500 \
        --budget 15 --only-valid --out leads.csv

    python -m coldstack.cli providers
    python -m coldstack.cli genkey
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .pipeline.build_list import build_list
from .pipeline.export import write_csv
from .providers.base import SearchQuery
from .providers.registry import email_finders, search_providers, verifiers
from .vault import generate_master_key


def _headcount(spec: str | None) -> tuple[int | None, int | None]:
    if not spec:
        return None, None
    if "-" in spec:
        lo, _, hi = spec.partition("-")
        return int(lo) if lo else None, int(hi) if hi else None
    return int(spec), None


def _load_suppression(path: str | None) -> tuple[set[str], set[str]]:
    """One entry per line. '@example.com' or 'example.com' suppresses a whole domain."""
    if not path:
        return set(), set()
    emails, domains = set(), set()
    for raw in open(path, encoding="utf-8"):
        line = raw.strip().lower()
        if not line or line.startswith("#"):
            continue
        (domains if "@" not in line.strip("@") else emails).add(line.lstrip("@"))
    return emails, domains


async def _build(args: argparse.Namespace) -> int:
    lo, hi = _headcount(args.headcount)
    query = SearchQuery(
        titles=args.title or [], seniorities=args.seniority or [],
        departments=args.department or [], industries=args.industry or [],
        countries=args.country or [], headcount_min=lo, headcount_max=hi,
        tech_any=args.tech or [], keywords=args.keyword or [], limit=args.limit,
    )

    if args.demo:
        search, search_key = search_providers()["fake"](), ""
        finders = {"fake": email_finders()["fake"]()}
        finder_keys = {"fake": "x"}
        verifier, verifier_key = verifiers()["fake"](), None
    else:
        apollo_key = os.getenv("APOLLO_API_KEY", "")
        hunter_key = os.getenv("HUNTER_API_KEY", "")
        if not apollo_key:
            print("APOLLO_API_KEY is not set (or use --demo)", file=sys.stderr)
            return 2
        search, search_key = search_providers()["apollo"](), apollo_key
        finders, finder_keys = {}, {}
        if hunter_key:
            finders["hunter"] = email_finders()["hunter"]()
            finder_keys["hunter"] = hunter_key
        # Apollo reveal costs credits, so it sits behind the cheaper finders and is
        # only reached when they miss.
        finders["apollo_enrich"] = email_finders()["apollo_enrich"]()
        finder_keys["apollo_enrich"] = apollo_key
        verifier = verifiers()["hunter"]() if hunter_key else None
        verifier_key = hunter_key or None

    sup_e, sup_d = _load_suppression(args.suppress)
    leads, report = await build_list(
        query, search=search, search_key=search_key,
        finders=finders, finder_keys=finder_keys,
        verifier=verifier, verifier_key=verifier_key,
        suppressed_emails=sup_e, suppressed_domains=sup_d,
        budget_usd=args.budget, concurrency=args.concurrency,
    )

    written = write_csv(leads, args.out, only_valid=args.only_valid,
                        include_catch_all=args.include_catch_all)
    print(report.summary())
    print(f"\nwrote {written} rows -> {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="coldstack")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="search -> find emails -> verify -> CSV")
    b.add_argument("--demo", action="store_true", help="use fake providers, no keys, no cost")
    b.add_argument("--title", action="append")
    b.add_argument("--seniority", action="append")
    b.add_argument("--department", action="append")
    b.add_argument("--industry", action="append")
    b.add_argument("--country", action="append")
    b.add_argument("--tech", action="append")
    b.add_argument("--keyword", action="append")
    b.add_argument("--headcount", help="e.g. 20-500")
    b.add_argument("--limit", type=int, default=100)
    b.add_argument("--budget", type=float, default=None, help="hard stop, USD")
    b.add_argument("--concurrency", type=int, default=8)
    b.add_argument("--suppress", help="file of emails/domains to exclude")
    b.add_argument("--only-valid", action="store_true")
    b.add_argument("--include-catch-all", action="store_true")
    b.add_argument("--out", default="leads.csv")

    sub.add_parser("providers", help="list registered adapters")
    sub.add_parser("genkey", help="generate a CREDENTIAL_MASTER_KEY")

    args = ap.parse_args(argv)
    if args.cmd == "genkey":
        print(generate_master_key())
        return 0
    if args.cmd == "providers":
        for label, table in (("search", search_providers()), ("finder", email_finders()),
                             ("verifier", verifiers())):
            for name, cls in sorted(table.items()):
                cost = getattr(cls, "est_unit_cost_usd", None)
                print(f"{label:9} {name:16} " + (f"~${cost}/call" if cost is not None else ""))
        return 0
    return asyncio.run(_build(args))


if __name__ == "__main__":
    raise SystemExit(main())
