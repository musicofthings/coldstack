#!/usr/bin/env python3
"""Structural checks for the hand-written landing page.

No build step means nothing else catches a malformed document, and two of these
mistakes have already shipped once: a wrapper that closed a <body> it never opened,
and a font stylesheet left outside <head>. A CSS custom property missing from the bare
:root block is the third - it renders one theme unstyled.
"""
from __future__ import annotations

import pathlib
import re
import sys

REQUIRED_TAGS = ("<!doctype html>", "<html lang=", "<head>", "</head>",
                 "<body>", "</body>", "</html>", "<title>")


def check(path: pathlib.Path) -> list[str]:
    s = path.read_text()
    problems = [f"missing {t}" for t in REQUIRED_TAGS if t not in s]

    if s.count("<body>") != 1 or s.count("</body>") != 1:
        problems.append("body tag is not balanced")

    if "</head>" in s:
        head_end = s.index("</head>")
        if "<style>" in s and s.index("<style>") > head_end:
            problems.append("<style> sits outside <head>")
        if "fonts.googleapis.com" in s and s.index("fonts.googleapis.com") > head_end:
            problems.append("font stylesheet sits outside <head>")

    root = re.search(r":root\{(.*?)\n\}", s, re.S)
    if root:
        used = set(re.findall(r"var\((--[a-z-]+)", s))
        declared = set(re.findall(r"(--[a-z-]+):", root.group(1)))
        undeclared = sorted(used - declared)
        if undeclared:
            problems.append(f"CSS variables not declared in the bare :root block: {undeclared}")
    else:
        problems.append("no bare :root block found")

    return problems


def main() -> int:
    path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "index.html")
    if not path.exists():
        print(f"FAIL: {path} does not exist")
        return 1
    problems = check(path)
    if problems:
        print("\n".join(f"FAIL: {p}" for p in problems))
        return 1
    print(f"{path} OK ({len(path.read_text()):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
