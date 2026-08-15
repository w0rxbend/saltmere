#!/usr/bin/env python3
"""Mechanical house-style checks for articles.

A language model is good at judging whether a paragraph is well argued and bad
at reliably noticing that the word "simply" survived in line 128. This script
handles the second category — the checks that are exact, cheap and worth running
on every push once the whole corpus has been converted.

It checks the rules from STYLE.md that can be decided without judgement:

  * second person ("you", "your", "we", "let's")
  * filler words ("just", "simply", "actually", "really", "basically",
    "obviously", "of course", "it turns out")
  * a "**Gist.**" opening paragraph
  * a "## Pitfalls" section
  * marketing superlatives

Prose inside code blocks and inline code is exempt: `git rebase --just-in-case`
is a command, not filler.

    python3 tools/check_style.py                       # whole corpus
    python3 tools/check_style.py _articles/x/y.md ...  # named files
"""
import glob
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
FENCED = re.compile(r"^```.*?^```", re.S | re.M)
INLINE_CODE = re.compile(r"`[^`\n]+`")
RAW = re.compile(r"\{%-?\s*raw\s*-?%\}.*?\{%-?\s*endraw\s*-?%\}", re.S)
LINK_TARGET = re.compile(r"\]\([^)]*\)")

SECOND_PERSON = r"\b(you|your|yours|you're|you'll|you've|we|we're|we'll|us|our|ours|let's)\b"
FILLER = r"\b(just|simply|actually|really|basically|obviously|of course|it turns out|needless to say)\b"
HYPE = r"\b(blazing[- ]fast|game[- ]changer|magic(al)?|effortless|seamless|revolutionary|supercharged)\b"

CHECKS = [
    ("second person", SECOND_PERSON),
    ("filler", FILLER),
    ("hype", HYPE),
]


def prose_of(text):
    """Body text with code, raw blocks and link targets blanked out, preserving
    offsets so reported line numbers stay accurate."""
    body = FRONT_MATTER.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    for pattern in (FENCED, RAW, INLINE_CODE, LINK_TARGET):
        body = pattern.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), body)
    return body


def check(path):
    text = open(path, encoding="utf-8").read()
    prose = prose_of(text)
    problems = []

    for name, pattern in CHECKS:
        for m in re.finditer(pattern, prose, re.I):
            line = prose.count("\n", 0, m.start()) + 1
            context = text.split("\n")[line - 1].strip()
            problems.append((line, name, m.group(0), context[:90]))

    # The title and summary are rendered on cards, the home page and the
    # article header, so they are prose too — and being inside the front matter
    # is exactly why an editing pass tends to miss them.
    fm = FRONT_MATTER.match(text)
    if fm:
        meta = yaml.safe_load(fm.group(1)) or {}
        for field in ("title", "summary"):
            value = meta.get(field) or ""
            for name, pattern in (("second person", SECOND_PERSON), ("hype", HYPE)):
                m = re.search(pattern, value, re.I)
                if m:
                    line = text.count("\n", 0, fm.group(0).find(str(value))) + 1
                    problems.append((line, f"{name} in {field}", m.group(0), value[:90]))

    if not re.search(r"^\*\*Gist\.\*\*", prose, re.M):
        problems.append((0, "structure", "missing **Gist.** opener", ""))
    if not re.search(r"^##\s+Pitfalls\s*$", prose, re.M):
        problems.append((0, "structure", "missing ## Pitfalls section", ""))

    return sorted(problems)


def main():
    targets = sys.argv[1:] or sorted(glob.glob(os.path.join(ROOT, "_articles", "*", "*.md")))
    failed = 0
    total_problems = 0

    for path in targets:
        problems = check(path)
        if problems:
            failed += 1
            total_problems += len(problems)
            rel = os.path.relpath(path, ROOT)
            for line, name, hit, context in problems:
                where = f"{rel}:{line}" if line else rel
                print(f"{where}: {name}: {hit}" + (f"  |  {context}" if context else ""))

    print(f"\nchecked {len(targets)} article(s): "
          f"{failed} with problems, {total_problems} problem(s) total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
