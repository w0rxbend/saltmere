#!/usr/bin/env python3
"""Report how far the corpus conversion to STYLE.md has got, by track.

Converting 459 articles is a multi-session job. This gives the next session (or
the next person) a one-command answer to "what is left?".

    python3 tools/style_progress.py
"""
import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKER = os.path.join(ROOT, "tools", "check_style.py")


def main():
    tracks = sorted(
        d for d in os.listdir(os.path.join(ROOT, "_articles"))
        if os.path.isdir(os.path.join(ROOT, "_articles", d))
    )

    total = done = 0
    rows = []
    for track in tracks:
        files = sorted(glob.glob(os.path.join(ROOT, "_articles", track, "*.md")))
        clean = 0
        for f in files:
            # check_style exits non-zero when a file has any problem
            r = subprocess.run([sys.executable, CHECKER, f],
                               capture_output=True, text=True)
            if r.returncode == 0:
                clean += 1
        rows.append((track, clean, len(files)))
        total += len(files)
        done += clean

    width = max(len(t) for t, _, _ in rows)
    print(f"{'track'.ljust(width)}  converted     bar")
    for track, clean, n in rows:
        pct = clean / n if n else 0
        bar = "#" * round(pct * 24)
        print(f"{track.ljust(width)}  {clean:>3}/{n:<3}  {pct:5.0%}  {bar}")
    print(f"\n{'TOTAL'.ljust(width)}  {done:>3}/{total:<3}  {done/total:5.0%}")


if __name__ == "__main__":
    main()
