---
layout: default
title: Home
---

{%- assign all = site.articles | sort: "date" | reverse -%}
{%- assign newest = all | first -%}

<section class="hero">
  <p class="kicker">Feed live · last drop {{ newest.date | date: "%Y.%m.%d" }}</p>
  <h1>Saltmere</h1>
  <p class="lede">{{ site.tagline }}</p>
  <p class="note muted">Short, implementable articles researched from primary sources and engineering blogs, then published here automatically — twice a day, 09:00 &amp; 21:00 Europe/Kyiv.</p>

  <div class="hud-stats">
    <div class="hud-stat"><span class="v">{{ all.size }}</span><span class="k">Articles</span></div>
    <div class="hud-stat"><span class="v">{{ site.tracks.size }}</span><span class="k">Tracks</span></div>
    <div class="hud-stat"><span class="v">2×</span><span class="k">Per day</span></div>
    <div class="hud-stat"><span class="v">{{ newest.date | date: "%b %-d" }}</span><span class="k">Latest</span></div>
  </div>
</section>

<nav class="filters" aria-label="Filter by track">
  <button class="chip is-active" type="button" data-filter="all">All <span class="n">{{ all.size }}</span></button>
  {%- for t in site.tracks %}
  {%- assign n = all | where: "track", t.id | size %}
  <button class="chip" type="button" data-filter="{{ t.id }}" data-track="{{ t.id }}">
    <span class="dot" aria-hidden="true"></span>{{ t.name }} <span class="n">{{ n }}</span>
  </button>
  {%- endfor %}
</nav>

<section class="home-section" id="latest">
  <div class="section-head">
    <h2>Latest drop</h2>
    <span class="rule"></span>
    <span class="count">newest first</span>
  </div>
  <ul class="cards">
    {%- for a in all limit: 6 %}
    {%- assign track = site.tracks | where: "id", a.track | first %}
    <li class="card" data-track="{{ a.track }}">
      <a href="{{ a.url | relative_url }}">
        <span class="card-track">{{ track.name | default: a.track }}</span>
        <span class="card-title">{{ a.title }}</span>
        {%- if a.summary %}<span class="card-summary">{{ a.summary | strip_html | truncate: 130 }}</span>{% endif %}
        <span class="card-meta">
          <span>{{ a.date | date: "%Y.%m.%d" }}</span>
          {%- if a.reading_time %}<span>{{ a.reading_time }} min</span>{% endif %}
        </span>
      </a>
    </li>
    {%- endfor %}
  </ul>
</section>

{%- for t in site.tracks %}
{%- assign items = all | where: "track", t.id %}
<section class="home-section track" id="{{ t.id }}" data-track="{{ t.id }}">
  <div class="section-head">
    <h2>{{ t.name }}</h2>
    <span class="rule"></span>
    <span class="count">{{ items.size }} articles</span>
  </div>
  <p class="track-blurb">{{ t.blurb }}</p>

  {%- if items.size > 0 %}
  <ul class="list" id="list-{{ t.id }}">
    {%- for a in items %}
    <li{% if forloop.index > 8 %} class="is-overflow"{% endif %}>
      <a class="row" href="{{ a.url | relative_url }}">
        <span class="row-date">{{ a.date | date: "%Y.%m.%d" }}</span>
        <span class="row-main">
          <span class="row-title">{{ a.title }}</span>
          {%- if a.summary %}<span class="row-summary">{{ a.summary | strip_html | truncate: 150 }}</span>{% endif %}
        </span>
        <span class="row-time">{% if a.reading_time %}{{ a.reading_time }} min{% endif %}</span>
      </a>
    </li>
    {%- endfor %}
  </ul>
  {%- if items.size > 8 %}
  <button class="hud-btn more-btn" type="button" data-more-for="list-{{ t.id }}">
    Show all {{ items.size }} in {{ t.name }} ▾
  </button>
  {%- endif %}
  {%- else %}
  <p class="muted">No articles yet — the first one lands on the next run.</p>
  {%- endif %}
</section>
{%- endfor %}
