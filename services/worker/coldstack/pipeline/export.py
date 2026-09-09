"""CSV export.

Deliberately includes `email_status` and `email_source`. A list that hides which
addresses are catch-all or risky is how a domain gets burned - the user needs to be
able to filter before they send, not after they bounce.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from .build_list import Lead

COLUMNS = ["first_name", "last_name", "full_name", "title", "seniority", "department",
           "company", "domain", "industry", "headcount", "country", "linkedin_url",
           "email", "email_status", "email_source", "cost_usd"]


def write_csv(leads: Iterable[Lead], path: str | Path, *,
              only_valid: bool = False, include_catch_all: bool = False) -> int:
    rows = []
    for l in leads:
        if not l.email:
            continue
        if only_valid and l.email_status != "valid":
            if not (include_catch_all and l.email_status == "catch_all"):
                continue
        row = {c: getattr(l, c) for c in COLUMNS}
        # Binary floats accumulate visible noise (0.0045000000000000005).
        # A CSV opened in a spreadsheet should show money, not artefacts.
        row["cost_usd"] = f'{l.cost_usd:.5f}'
        rows.append(row)

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)
