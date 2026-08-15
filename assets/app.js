/* ============================================================================
   Saltmere front-end.

   Four independent pieces, each guarded so a missing element on some page
   never breaks the others:

     1. theme      — dark/light toggle, remembered in localStorage
     2. tracksMenu — the "Tracks" dropdown in the header
     3. palette    — the ⌘K / Ctrl-K client-side search over search.json
     4. page bits  — home-page track filters, "show more" lists, and the
                     article reading-progress bar

   No build step and no dependencies: this file is loaded directly by the
   browser, so it is plain ES5-compatible JavaScript.
   ========================================================================= */
(function () {
  "use strict";

  var root = document.documentElement;
  root.classList.remove("no-js");

  /* ------------------------------------------------------------ 1. theme */

  var THEME_KEY = "saltmere-theme";

  function systemTheme() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light"
      : "dark";
  }

  function currentTheme() {
    return root.getAttribute("data-theme") || systemTheme();
  }

  function applyTheme(name) {
    root.setAttribute("data-theme", name);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", name === "light" ? "#eef1f5" : "#05070a");
    var btn = document.querySelector(".theme-toggle");
    if (btn) {
      btn.setAttribute("aria-label", name === "light" ? "Switch to dark theme" : "Switch to light theme");
      var glyph = btn.querySelector(".tt-glyph");
      if (glyph) glyph.textContent = name === "light" ? "◑" : "◐";
    }
  }

  applyTheme(currentTheme());

  var themeBtn = document.querySelector(".theme-toggle");
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      // Swap both palettes in one frame — see .theme-switching in the CSS.
      root.classList.add("theme-switching");
      applyTheme(next);
      window.requestAnimationFrame(function () {
        window.requestAnimationFrame(function () { root.classList.remove("theme-switching"); });
      });
      try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* private mode */ }
    });
  }

  /* ------------------------------------------------------ 2. tracks menu */

  var menu = document.querySelector(".tracks-menu");
  if (menu) {
    var menuBtn = menu.querySelector("button");

    var setMenu = function (open) {
      menu.setAttribute("data-open", open ? "true" : "false");
      if (menuBtn) menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
    };

    menuBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      setMenu(menu.getAttribute("data-open") !== "true");
    });

    document.addEventListener("click", function (e) {
      if (!menu.contains(e.target)) setMenu(false);
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") setMenu(false);
    });
  }

  /* ----------------------------------------------------------- 3. search */

  var palette = document.getElementById("palette");

  if (palette) {
    var input    = document.getElementById("palette-input");
    var resultsEl = document.getElementById("palette-results");
    var emptyEl  = document.getElementById("palette-empty");
    var countEl  = document.getElementById("palette-count");

    var index = null;        // the fetched article index, once loaded
    var loading = false;
    var selected = 0;        // highlighted row, for arrow-key navigation
    var lastFocus = null;    // element to restore focus to when we close

    var TRACK_NAMES = {};
    try {
      TRACK_NAMES = JSON.parse(palette.getAttribute("data-track-names") || "{}");
    } catch (e) { /* fall back to raw ids */ }

    function loadIndex() {
      if (index || loading) return Promise.resolve(index);
      loading = true;
      return fetch(palette.getAttribute("data-index"))
        .then(function (r) { return r.json(); })
        .then(function (data) {
          index = data.map(function (a) {
            // Pre-lowercase a single haystack string per article so that
            // typing a character does not re-lowercase 400+ records.
            a.hay = (a.t + " " + a.s + " " + (a.g || []).join(" ") + " " +
                     a.k + " " + (TRACK_NAMES[a.k] || "")).toLowerCase();
            a.tlow = a.t.toLowerCase();
            return a;
          });
          loading = false;
          return index;
        })
        .catch(function () {
          loading = false;
          if (emptyEl) {
            emptyEl.textContent = "search index unavailable";
            emptyEl.hidden = false;
          }
        });
    }

    /* Score one article against the search terms.
       Every term must appear somewhere, otherwise the article is dropped.
       Where it appears decides how strongly it ranks:
         title start > title anywhere > tag/summary/track. */
    function score(item, terms) {
      var total = 0;
      for (var i = 0; i < terms.length; i++) {
        var term = terms[i];
        if (item.hay.indexOf(term) === -1) return -1;
        var at = item.tlow.indexOf(term);
        if (at === 0) total += 12;
        else if (at > 0) total += 8 - (at > 30 ? 2 : 0);
        else total += 2;
      }
      // Nudge shorter titles up: an exact-topic article beats a passing mention.
      return total + Math.max(0, 40 - item.tlow.length) / 40;
    }

    function escapeHtml(s) {
      return s.replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
    }

    /* Wrap each matched term in <mark>, without letting the search text
       itself become markup. */
    function highlight(text, terms) {
      var safe = escapeHtml(text);
      if (!terms.length) return safe;
      var pattern = terms
        .slice()
        .sort(function (a, b) { return b.length - a.length; })
        .map(function (t) { return t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); })
        .join("|");
      return safe.replace(new RegExp("(" + pattern + ")", "gi"), "<mark>$1</mark>");
    }

    function render(query) {
      if (!index) return;

      var terms = query.toLowerCase().split(/\s+/).filter(Boolean);
      var hits;

      if (!terms.length) {
        hits = index.slice(0, 12);           // empty query → most recent
      } else {
        hits = index
          .map(function (it) { return { it: it, sc: score(it, terms) }; })
          .filter(function (x) { return x.sc >= 0; })
          .sort(function (a, b) { return b.sc - a.sc || (a.it.d < b.it.d ? 1 : -1); })
          .slice(0, 30)
          .map(function (x) { return x.it; });
      }

      selected = 0;
      resultsEl.innerHTML = hits.map(function (a, i) {
        return '<li data-track="' + a.k + '"' + (i === 0 ? ' class="is-sel"' : "") + ">" +
               '<a href="' + a.u + '">' +
                 '<span class="pr-title">' + highlight(a.t, terms) + "</span>" +
                 '<span class="pr-meta"><span class="pr-track">' +
                   escapeHtml(TRACK_NAMES[a.k] || a.k) +
                 "</span> · " + a.d + (a.r ? " · " + a.r + " min" : "") + "</span>" +
               "</a></li>";
      }).join("");

      if (emptyEl) {
        emptyEl.hidden = hits.length > 0;
        emptyEl.textContent = "no match for “" + query + "”";
      }
      if (countEl) {
        countEl.textContent = terms.length
          ? hits.length + (hits.length === 30 ? "+" : "") + " hits"
          : index.length + " articles";
      }
    }

    function moveSelection(delta) {
      var rows = resultsEl.children;
      if (!rows.length) return;
      rows[selected] && rows[selected].classList.remove("is-sel");
      selected = (selected + delta + rows.length) % rows.length;
      rows[selected].classList.add("is-sel");
      rows[selected].scrollIntoView({ block: "nearest" });
    }

    function openPalette() {
      lastFocus = document.activeElement;
      palette.setAttribute("data-open", "true");
      document.body.style.overflow = "hidden";
      loadIndex().then(function () { render(input.value.trim()); });
      input.focus();
      input.select();
    }

    function closePalette() {
      palette.setAttribute("data-open", "false");
      document.body.style.overflow = "";
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    }

    Array.prototype.forEach.call(document.querySelectorAll("[data-search-open]"), function (el) {
      el.addEventListener("click", function (e) { e.preventDefault(); openPalette(); });
    });

    palette.addEventListener("click", function (e) {
      if (e.target === palette) closePalette();   // click the dim backdrop
    });

    input.addEventListener("input", function () { render(input.value.trim()); });

    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); moveSelection(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); moveSelection(-1); }
      else if (e.key === "Enter") {
        var row = resultsEl.children[selected];
        var link = row && row.querySelector("a");
        if (link) { e.preventDefault(); window.location.href = link.getAttribute("href"); }
      }
    });

    document.addEventListener("keydown", function (e) {
      var open = palette.getAttribute("data-open") === "true";

      if ((e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        open ? closePalette() : openPalette();
        return;
      }
      if (e.key === "Escape" && open) { e.preventDefault(); closePalette(); return; }

      // Bare "/" opens search, but not while the visitor is typing elsewhere.
      if (e.key === "/" && !open) {
        var tag = (e.target.tagName || "").toLowerCase();
        if (tag === "input" || tag === "textarea" || e.target.isContentEditable) return;
        e.preventDefault();
        openPalette();
      }
    });
  }

  /* ------------------------------------------------------- 4. page bits */

  /* Home-page track filter chips: hide every track section except the
     chosen one. Pure DOM work — no page reload, no server involved. */
  var filterBar = document.querySelector(".filters");
  if (filterBar) {
    var sections = document.querySelectorAll(".home-section.track");

    filterBar.addEventListener("click", function (e) {
      var chip = e.target.closest ? e.target.closest(".chip") : null;
      if (!chip || !filterBar.contains(chip)) return;
      e.preventDefault();

      var want = chip.getAttribute("data-filter");

      Array.prototype.forEach.call(filterBar.querySelectorAll(".chip"), function (c) {
        c.classList.toggle("is-active", c === chip);
      });
      Array.prototype.forEach.call(sections, function (s) {
        s.hidden = want !== "all" && s.id !== want;
      });

      var latest = document.getElementById("latest");
      if (latest) latest.hidden = want !== "all";
    });
  }

  /* "Show all N" buttons on the long per-track lists. Each list ships every
     article in the HTML (good for search engines and no-JS visitors); we
     just collapse the overflow until asked. */
  Array.prototype.forEach.call(document.querySelectorAll("[data-more-for]"), function (btn) {
    var list = document.getElementById(btn.getAttribute("data-more-for"));
    if (!list) return;

    var hidden = list.querySelectorAll("li.is-overflow");
    if (!hidden.length) { btn.hidden = true; return; }

    btn.addEventListener("click", function () {
      Array.prototype.forEach.call(hidden, function (li) { li.classList.remove("is-overflow"); });
      btn.hidden = true;
    });
  });

  /* Reading-progress bar on article pages. */
  var bar = document.querySelector(".progress");
  if (bar) {
    var article = document.querySelector(".article");
    var tick = function () {
      var start = article.offsetTop;
      var span = article.offsetHeight - window.innerHeight;
      var pct = span <= 0 ? 100 : ((window.scrollY - start) / span) * 100;
      bar.style.width = Math.min(100, Math.max(0, pct)) + "%";
    };
    var queued = false;
    var onScroll = function () {
      if (queued) return;
      queued = true;
      window.requestAnimationFrame(function () { tick(); queued = false; });
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    tick();
  }
})();
