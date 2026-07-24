---
layout: default
title: Home
---

<section class="hero">
  <p class="kicker">Engineering journal</p>
  <h1>Saltmere</h1>
  <p class="lede">{{ site.tagline }}</p>
  <p class="muted">A fresh batch of short, implementable articles lands twice a day — 09:00 &amp; 21:00 Europe/Kyiv — researched from primary sources and engineering blogs, then published here automatically.</p>
</section>

{% assign all = site.articles | sort: "date" | reverse %}

<section class="home-section">
  <div class="section-head">
    <h2>Latest</h2>
    <span class="rule"></span>
  </div>
  <ul class="cards">
    {% for a in all limit: 6 %}
    {% assign track = site.tracks | where: "id", a.track | first %}
    <li class="card">
      <a href="{{ a.url | relative_url }}">
        <span class="card-track">{{ track.name | default: a.track }}</span>
        <span class="card-title">{{ a.title }}</span>
        {% if a.summary %}<span class="card-summary">{{ a.summary | truncate: 130 }}</span>{% endif %}
        <span class="card-date">{{ a.date | date: "%b %-d, %Y" }}</span>
      </a>
    </li>
    {% endfor %}
  </ul>
</section>

{% for t in site.tracks %}
<section class="home-section track" id="{{ t.id }}">
  <div class="section-head">
    <h2>{{ t.name }}</h2>
    <span class="rule"></span>
  </div>
  <p class="track-blurb">{{ t.blurb }}</p>
  {% assign items = all | where: "track", t.id %}
  {% if items.size > 0 %}
  <ul class="list">
    {% for a in items %}
    <li>
      <a class="list-title" href="{{ a.url | relative_url }}">{{ a.title }}</a>
      <span class="when"> · {{ a.date | date: "%b %-d" }}</span>
      {% if a.summary %}<div class="list-summary">{{ a.summary | truncate: 170 }}</div>{% endif %}
    </li>
    {% endfor %}
  </ul>
  {% else %}
  <p class="muted">No articles yet — first one lands on the next run.</p>
  {% endif %}
</section>
{% endfor %}
