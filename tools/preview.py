#!/usr/bin/env python3
"""Minimal local preview builder for Saltmere.

Jekyll needs Ruby, which is not installed on this machine. This script renders
just enough of the site (the Liquid subset the layouts actually use) into
_preview/ so the design can be checked in a browser. It is a development aid
only — GitHub Pages still builds the real site with Jekyll.

    python3 tools/preview.py && python3 -m http.server -d _preview 8080
"""
import json
import os
import re
import shutil
import sys
from datetime import date, datetime

import markdown as md
import yaml
from liquid import Environment
from liquid import FileSystemLoader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "_preview")
BASEURL = ""  # served from / locally, unlike the real /saltmere

FM_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


def read_front_matter(path):
    raw = open(path, encoding="utf-8").read()
    m = FM_RE.match(raw)
    if not m:
        return {}, raw
    return yaml.safe_load(m.group(1)) or {}, m.group(2)


# --------------------------------------------------------------- filters


def f_relative_url(v):
    v = str(v or "")
    return BASEURL + v if v.startswith("/") else v


def f_jsonify(v):
    if isinstance(v, (date, datetime)):
        v = v.isoformat()
    return json.dumps(v, ensure_ascii=False)


STRFTIME = {"%-d": "%d", "%-m": "%m"}


def f_date(v, fmt):
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return v
    if not isinstance(v, (date, datetime)):
        return v
    out = fmt
    for k in STRFTIME:
        out = out.replace(k, STRFTIME[k])
    s = v.strftime(out)
    if "%-d" in fmt:
        s = s.replace(v.strftime("%d"), str(v.day), 1)
    return s


def f_date_to_xmlschema(v):
    if isinstance(v, date) and not isinstance(v, datetime):
        v = datetime(v.year, v.month, v.day)
    return v.isoformat() + "Z"


def f_where(seq, key, val=None):
    seq = seq or []
    if val is None:
        return [x for x in seq if x.get(key)]
    return [x for x in seq if x.get(key) == val]


def f_strip_html(v):
    return re.sub(r"<[^>]+>", "", str(v))


def build_env():
    env = Environment(loader=FileSystemLoader(ROOT))
    env.filters["relative_url"] = f_relative_url
    env.filters["absolute_url"] = f_relative_url
    env.filters["jsonify"] = f_jsonify
    env.filters["date"] = f_date
    env.filters["date_to_xmlschema"] = f_date_to_xmlschema
    env.filters["where"] = f_where
    env.filters["strip_html"] = f_strip_html
    return env


# ----------------------------------------------------------------- site


def load_site():
    cfg = yaml.safe_load(open(os.path.join(ROOT, "_config.yml"), encoding="utf-8"))
    articles = []
    for track_dir in sorted(os.listdir(os.path.join(ROOT, "_articles"))):
        d = os.path.join(ROOT, "_articles", track_dir)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".md"):
                continue
            fm, body = read_front_matter(os.path.join(d, name))
            fm["body_md"] = body
            fm["url"] = f"/articles/{track_dir}/{name[:-3]}/"
            fm.setdefault("track", track_dir)
            articles.append(fm)
    articles.sort(key=lambda a: (str(a.get("date")), a["url"]))
    cfg["articles"] = articles
    return cfg


def render_layout(env, site, layout, page, content):
    """Render `content` through the named layout, following `layout:` chains."""
    while layout:
        src, meta = read_layout(layout)
        tmpl = env.from_string(src)
        content = tmpl.render(site=site, page=page, content=content)
        layout = meta.get("layout")
    return content


_layout_cache = {}


def read_layout(name):
    if name not in _layout_cache:
        meta, body = read_front_matter(os.path.join(ROOT, "_layouts", f"{name}.html"))
        # {% seo %} is provided by a Jekyll plugin we do not emulate.
        body = re.sub(r"\{%-?\s*seo\s*-?%\}", "<title>Saltmere</title>", body)
        _layout_cache[name] = (body, meta)
    return _layout_cache[name]


def write(path, html):
    full = os.path.join(OUT, path.strip("/"), "index.html") if not path.endswith(
        ".json"
    ) else os.path.join(OUT, path.strip("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    open(full, "w", encoding="utf-8").write(html)


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    env = build_env()
    site = load_site()

    # assets
    shutil.copytree(os.path.join(ROOT, "assets"), os.path.join(OUT, "assets"))

    mdconv = md.Markdown(extensions=["fenced_code", "tables", "codehilite", "attr_list"])

    # home
    fm, body = read_front_matter(os.path.join(ROOT, "index.md"))
    page = dict(fm)
    inner = env.from_string(body).render(site=site, page=page)
    write("/", render_layout(env, site, fm.get("layout", "default"), page, inner))

    # search index
    fm, body = read_front_matter(os.path.join(ROOT, "search.json"))
    write("/search.json", env.from_string(body).render(site=site, page=fm))

    # track pages
    for name in sorted(os.listdir(os.path.join(ROOT, "tracks"))):
        fm, _ = read_front_matter(os.path.join(ROOT, "tracks", name))
        html = render_layout(env, site, fm["layout"], fm, "")
        write(fm["permalink"], html)

    # articles
    raw_tag = re.compile(r"\{%-?\s*(end)?raw\s*-?%\}\n?")
    for a in site["articles"]:
        mdconv.reset()
        # Jekyll consumes {% raw %} markers; strip them so the preview shows
        # the same thing the published page does.
        content = mdconv.convert(raw_tag.sub("", a["body_md"]))
        html = render_layout(env, site, "article", a, content)
        write(a["url"], html)

    print(f"built {len(site['articles'])} articles → {OUT}")


if __name__ == "__main__":
    sys.exit(main())
