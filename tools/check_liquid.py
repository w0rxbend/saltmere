#!/usr/bin/env python3
"""Fail the build early when an article contains template syntax Jekyll will
choke on.

Why this exists
---------------
Jekyll runs every article's Markdown through Liquid before converting it to
HTML. Liquid claims two pieces of syntax for itself:

    {% ... %}   a tag
    {{ ... }}   an output expression

Plenty of the tools these articles cover use exactly the same braces for their
own templating — GitHub Actions (${{ secrets.X }}), Prometheus alert
annotations ({{ $labels.job }}), Vault ({{username}}), Argo Workflows
({{inputs.parameters.x}}), Helm, Jinja, Klipper macros. When such a snippet
lands in an article unescaped, one of two things happens:

  * an unterminated {% ... %} aborts the whole site build, or
  * a {{ ... }} silently evaluates to nothing, so the published code sample is
    quietly wrong.

The fix in an article is to wrap the snippet:

    {% raw %}
    ```yaml
    token: ${{ secrets.WOKWI_CLI_TOKEN }}
    ```
    {% endraw %}

Deliberate Liquid in prose — {{ site.baseurl }} in a cross-link — is expected
and allowed.

Run it directly, or let the Pages workflow run it before `jekyll build`:

    python3 tools/check_liquid.py
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n?", re.S)
RAW_BLOCK = re.compile(r"\{%-?\s*raw\s*-?%\}.*?\{%-?\s*endraw\s*-?%\}", re.S)
OUTPUT_TAG = re.compile(r"\{\{.*?\}\}", re.S)
OPEN_TAG = re.compile(r"\{%")

# Liquid constructs the site legitimately uses inside article prose.
ALLOWED = ("site.", "page.", "content")


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


HYPHEN_RAW = re.compile(r"\{%-\s*(?:end)?raw\s*-%\}")
RAW_OPEN = re.compile(r"\{%\s*raw\s*%\}")
RAW_CLOSE = re.compile(r"\{%\s*endraw\s*%\}")


def check(path):
    text = open(path, encoding="utf-8").read()
    body = FRONT_MATTER.sub("", text)

    problems = []

    # Liquid 4.0.4 (what GitHub Pages runs) parses `raw` with a tokenizer that
    # matches only `{% raw %}` / `{% endraw %}`. The whitespace-control form
    # `{%- endraw -%}` is never recognised as closing the block, so the build
    # dies with "'raw' tag was never closed" — and the tags still look balanced
    # to a naive count, which is why this is checked explicitly.
    for m in HYPHEN_RAW.finditer(body):
        problems.append((line_of(body, m.start()),
                         f"{m.group(0)} — use the plain form; Liquid 4.0.4 does not "
                         "accept whitespace-control hyphens on raw/endraw"))

    opens, closes = len(RAW_OPEN.findall(body)), len(RAW_CLOSE.findall(body))
    if opens != closes:
        problems.append((0, f"unbalanced raw tags: {opens} raw, {closes} endraw"))

    # Blank out {% raw %} regions but keep the byte offsets so reported line
    # numbers still match the real file.
    masked = RAW_BLOCK.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), body)

    for m in OUTPUT_TAG.finditer(masked):
        inner = m.group(0)[2:-2].strip()
        if not any(inner.startswith(a) for a in ALLOWED):
            problems.append((line_of(masked, m.start()), m.group(0)[:60]))

    for m in OPEN_TAG.finditer(masked):
        tail = masked[m.start():m.start() + 200]
        if "%}" not in tail:
            problems.append((line_of(masked, m.start()), tail.split("\n")[0][:60]))

    return problems


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "_articles", "*", "*.md")))
    failed = 0

    for path in files:
        problems = check(path)
        if problems:
            failed += 1
            rel = os.path.relpath(path, ROOT)
            for line, snippet in problems:
                print(f"{rel}:{line}: unescaped template syntax: {snippet}")

    print(f"\nchecked {len(files)} articles, {failed} with problems")

    if failed:
        print(
            "\nJekyll will read the above as Liquid and either fail the build or\n"
            "render the snippet as nothing. Wrap the surrounding code block in\n"
            "{% raw %} ... {% endraw %} — see the header of tools/check_liquid.py."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
