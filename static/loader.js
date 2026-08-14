// A page restored from the back/forward cache (e.g. tapping "back" after
// adding a product on another page) keeps whatever server-rendered HTML it
// had at the moment of navigating away — no network request happens, so
// newly added catalog items, changed statuses, etc. would silently look
// stale (missing from search, wrong values) without ever erroring. Force a
// real reload whenever that happens so the page always reflects the
// current database. This runs unconditionally, ahead of every other
// script on the page, since staleness on back-navigation is a site-wide
// concern, not specific to any one page's own script.
window.addEventListener("pageshow", function (e) {
  if (e.persisted) location.reload();
});

(function () {
  var loader = document.getElementById("pageLoader");
  if (!loader) return;

  var MIN_VISIBLE_MS = 450;
  // A large multipart upload (several full-size photos from a phone camera)
  // over a weak mobile connection can hang or get silently dropped by the
  // hosting proxy well short of any browser-level network error — with
  // nothing to hide it, the overlay we show on submit would then cover the
  // page forever. This is the failsafe: if no navigation has completed by
  // then, give up waiting and let the user see the page (and retry) again.
  var FAILSAFE_MS = 20000;
  var shownAt = Date.now();
  var failsafeTimer = null;

  function hideInitial() {
    var elapsed = Date.now() - shownAt;
    setTimeout(function () {
      loader.classList.add("hidden");
    }, Math.max(0, MIN_VISIBLE_MS - elapsed));
  }

  if (document.readyState === "complete") {
    hideInitial();
  } else {
    window.addEventListener("load", hideInitial);
  }

  function showLoader() {
    loader.classList.remove("hidden");
    clearTimeout(failsafeTimer);
    failsafeTimer = setTimeout(function () {
      loader.classList.add("hidden");
    }, FAILSAFE_MS);
  }

  // Restoring a page from the back/forward cache (e.g. tapping "back" after
  // a submit) fires no "load" event, so a loader left visible before
  // navigating away would otherwise stay stuck on return.
  window.addEventListener("pageshow", function (e) {
    if (e.persisted) {
      clearTimeout(failsafeTimer);
      loader.classList.add("hidden");
    }
  });

  // Show the wheel again for any same-page navigation: clicking a link to
  // another page, or submitting a form (adding a trip, saving a filter,
  // running the Yclients import — anything that waits on the server).
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest("a[href]");
    if (!a || a.target === "_blank" || a.hasAttribute("download")) return;
    var href = a.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#" || href.indexOf("mailto:") === 0 || href.indexOf("tel:") === 0) return;
    if (a.origin !== window.location.origin) return;
    showLoader();
  });

  document.addEventListener("submit", function (e) {
    if (e.target && e.target.tagName === "FORM" && !e.defaultPrevented) {
      showLoader();
    }
  });
})();

// Restores scroll position and open <details> panels after a form submit
// or link click reloads the page — without this, every status change /
// delete / etc. lands back at the very top, which is especially annoying
// on pages with several collapsible panels (Мои задачи, Заявки на
// снабжение, ...): the user has to re-expand the panel and scroll back
// down to wherever they were working every single time.
(function () {
  var STORAGE_PREFIX = "scrollState:";

  function storageKey() {
    return STORAGE_PREFIX + location.pathname;
  }

  // <details> panels aren't given ids, so identify one by its own summary
  // text — every collapsible panel on a given page has a distinct heading
  // ("Мои задачи", "Заявки на снабжение", ...), which is more stable
  // across template edits than relying on DOM order.
  function detailsKey(el, index) {
    var summary = el.querySelector("summary");
    return summary ? summary.textContent.trim() : "details-" + index;
  }

  function save() {
    var open = [];
    document.querySelectorAll("details").forEach(function (el, i) {
      if (el.open) open.push(detailsKey(el, i));
    });
    try {
      sessionStorage.setItem(storageKey(), JSON.stringify({y: window.scrollY, open: open}));
    } catch (e) {
      // Private-browsing storage quota, or disabled entirely — losing the
      // scroll restore is harmless, so just skip it.
    }
  }

  function restore() {
    var raw;
    try {
      raw = sessionStorage.getItem(storageKey());
      sessionStorage.removeItem(storageKey());
    } catch (e) {
      return;
    }
    if (!raw) return;
    var state;
    try {
      state = JSON.parse(raw);
    } catch (e) {
      return;
    }
    document.querySelectorAll("details").forEach(function (el, i) {
      if (state.open && state.open.indexOf(detailsKey(el, i)) !== -1) el.open = true;
    });
    if (typeof state.y !== "number") return;
    // Two rAFs: the first lets the browser apply the layout change from
    // just-opened <details> panels, the second scrolls only once that's
    // actually painted — a single frame can still be mid-reflow.
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        window.scrollTo(0, state.y);
      });
    });
  }

  restore();
  window.addEventListener("pagehide", save);
  window.addEventListener("beforeunload", save);
  // Also save on every toggle (not just before navigating away) so the
  // open/closed state survives even where pagehide/beforeunload don't
  // reliably fire (notably iOS Safari in some cases).
  document.addEventListener("toggle", function (e) {
    if (e.target && e.target.tagName === "DETAILS") save();
  }, true);
})();

// Shared "smart" text matching for the table search and combobox below:
// splits the query into words and requires every word to appear somewhere
// in the target text, in any order — so "лодки крепление" still matches
// "Крепление для лодки" even though the words come in the opposite order.
// Returns a score (lower = better match, for ranking) or -1 for no match:
// 0 = text starts with the exact query, 1 = text contains the exact query
// as one substring, 2+ = matched only as separate scattered words, ranked
// by how early those words appear.
function smartMatchScore(text, query) {
  if (!query) return 0;
  var words = query.split(/\s+/).filter(Boolean);
  if (!words.length) return 0;
  for (var i = 0; i < words.length; i++) {
    if (text.indexOf(words[i]) === -1) return -1;
  }
  if (text.indexOf(query) === 0) return 0;
  if (text.indexOf(query) !== -1) return 1;
  var sum = 0;
  for (var j = 0; j < words.length; j++) sum += text.indexOf(words[j]);
  return 2 + sum / 1e6;
}

// Instant client-side table search — an <input data-filter-table="#id">
// hides/shows the referenced table's <tbody> rows as you type, matching
// against each row's full text (name, article, address, whatever columns
// that table has), no server round-trip. An optional companion
// <select data-filter-status="#id"> (matched against each row's own
// data-status attribute, not free text — for an exact category like an
// order's status) combines with it: a row must satisfy both to stay
// visible. Used on the supply catalog/warehouse pages and the tuning
// orders list, but written generically so any table can opt into either
// or both. Exposed as window.initTableFilters(root) — called once for the
// whole document below, and again by any page that swaps a filtered
// table's markup in via fetch (e.g. Analytics' date range) so the fresh
// input/table pair gets wired up too.
function initTableFilters(root) {
  root = root || document;
  var textInputs = Array.prototype.slice.call(root.querySelectorAll("input[data-filter-table]"));
  var statusSelects = Array.prototype.slice.call(root.querySelectorAll("select[data-filter-status]"));
  if (!textInputs.length && !statusSelects.length) return;

  var groups = {};
  function groupFor(selector) {
    if (!groups[selector]) {
      var table = document.querySelector(selector);
      var tbody = table && table.querySelector("tbody");
      groups[selector] = {
        rows: tbody ? Array.prototype.slice.call(tbody.querySelectorAll("tr")) : [],
        textInput: null,
        statusSelect: null,
        noResults: document.querySelector(selector + "-no-results"),
      };
    }
    return groups[selector];
  }
  textInputs.forEach(function (input) {
    groupFor(input.getAttribute("data-filter-table")).textInput = input;
  });
  statusSelects.forEach(function (select) {
    groupFor(select.getAttribute("data-filter-status")).statusSelect = select;
  });

  // A row's plain textContent pulls in every <option> of any <select> it
  // contains, not just the one currently selected — e.g. a per-row
  // "assign to project" dropdown lists every project, so every OTHER
  // project's name/client/boat would silently count toward this row's
  // searchable text too. Strip <select>s out before reading textContent so
  // a search only matches what's actually shown in the row.
  function rowSearchText(row) {
    var clone = row.cloneNode(true);
    clone.querySelectorAll("select").forEach(function (s) { s.remove(); });
    return clone.textContent.toLowerCase();
  }

  Object.keys(groups).forEach(function (selector) {
    var group = groups[selector];
    if (!group.rows.length) return;
    // Computed once, not per keystroke — the set of <option>s in a row's
    // dropdowns doesn't change as you type, only which one is selected.
    var searchText = group.rows.map(rowSearchText);
    var timer = null;

    function apply() {
      var q = group.textInput ? group.textInput.value.trim().toLowerCase() : "";
      var status = group.statusSelect ? group.statusSelect.value : "";
      var visible = 0;
      group.rows.forEach(function (row, i) {
        var textOk = !q || smartMatchScore(searchText[i], q) !== -1;
        var statusOk = !status || row.getAttribute("data-status") === status;
        var match = textOk && statusOk;
        row.classList.toggle("hidden", !match);
        if (match) visible += 1;
      });
      if (group.noResults) group.noResults.classList.toggle("hidden", visible !== 0 || (!q && !status));
    }

    if (group.textInput) {
      group.textInput.addEventListener("input", function () {
        clearTimeout(timer);
        timer = setTimeout(apply, 120);
      });
    }
    if (group.statusSelect) {
      group.statusSelect.addEventListener("change", apply);
    }
  });
}
initTableFilters();

// Searchable combobox — replaces a giant <select> (e.g. picking a catalog
// product to add to a tuning order, out of ~2800) with a text input that
// filters the same option list instantly as you type. Markup contract:
// a [data-combo] wrapper containing [data-combo-input] (visible text),
// [data-combo-value] (hidden input actually submitted), [data-combo-dropdown]
// holding .combo-option divs (each with data-value/data-label/data-search),
// and optional [data-combo-empty]/[data-combo-more] status lines.
(function () {
  var wrappers = document.querySelectorAll("[data-combo]");
  if (!wrappers.length) return;

  var MAX_VISIBLE = 50;

  wrappers.forEach(function (wrap) {
    var input = wrap.querySelector("[data-combo-input]");
    var hidden = wrap.querySelector("[data-combo-value]");
    var dropdown = wrap.querySelector("[data-combo-dropdown]");
    var emptyEl = wrap.querySelector("[data-combo-empty]");
    var moreEl = wrap.querySelector("[data-combo-more]");
    var options = Array.prototype.slice.call(wrap.querySelectorAll(".combo-option"));
    if (!input || !hidden || !dropdown || !options.length) return;

    var timer = null;

    function close() {
      dropdown.classList.add("hidden");
    }

    function render() {
      var q = input.value.trim().toLowerCase();
      if (!q) {
        close();
        return;
      }
      var scored = [];
      options.forEach(function (opt) {
        var score = smartMatchScore(opt.getAttribute("data-search"), q);
        if (score !== -1) scored.push({opt: opt, score: score});
      });
      // Best matches (exact prefix, then exact substring, then scattered
      // words ranked by how early they appear) float to the top of the
      // dropdown via CSS "order" — cheaper than moving DOM nodes around,
      // and .combo-dropdown is a flex column so it takes effect.
      scored.sort(function (a, b) { return a.score - b.score; });
      var matched = scored.length;
      var shown = Math.min(matched, MAX_VISIBLE);
      options.forEach(function (opt) {
        opt.classList.add("hidden");
        opt.style.order = "";
      });
      scored.slice(0, MAX_VISIBLE).forEach(function (item, idx) {
        item.opt.classList.remove("hidden");
        item.opt.style.order = idx;
      });
      if (emptyEl) emptyEl.classList.toggle("hidden", matched !== 0);
      if (moreEl) {
        if (matched > shown) {
          moreEl.textContent = "Показаны первые " + shown + " из " + matched + " — уточните запрос.";
          moreEl.classList.remove("hidden");
        } else {
          moreEl.classList.add("hidden");
        }
      }
      dropdown.classList.remove("hidden");
    }

    input.addEventListener("input", function () {
      // Any edit invalidates a previous pick until a fresh one is clicked —
      // otherwise a half-changed search term could silently submit the old
      // product_id.
      if (input.value !== input.dataset.selectedLabel) hidden.value = "";
      input.classList.remove("combo-input-error");
      clearTimeout(timer);
      timer = setTimeout(render, 120);
    });
    input.addEventListener("focus", function () {
      if (input.value.trim()) render();
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape") close();
    });

    options.forEach(function (opt) {
      opt.addEventListener("click", function () {
        var label = opt.getAttribute("data-label");
        hidden.value = opt.getAttribute("data-value");
        input.value = label;
        input.dataset.selectedLabel = label;
        close();
      });
    });

    document.addEventListener("click", function (e) {
      if (!wrap.contains(e.target)) close();
    });

    var form = wrap.closest("form");
    if (form) {
      form.addEventListener("submit", function (e) {
        if (!hidden.value) {
          e.preventDefault();
          input.classList.add("combo-input-error");
          input.focus();
        }
      });
    }
  });
})();

// Mobile burger menu — independent of the loader above (some pages, like
// the plain login screens, have the nav but not the page-loader overlay).
(function () {
  var toggle = document.getElementById("navToggle");
  var nav = document.getElementById("mainNav");
  if (!toggle || !nav) return;

  toggle.addEventListener("click", function () {
    var isOpen = nav.classList.toggle("open");
    toggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
  });
})();

// Registers the (deliberately no-op, no-caching) service worker so Chrome
// on Android considers the site installable — see /sw.js for why. Served
// from the domain root, not /static/, so its scope covers the whole site.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  });
}

// Push notification toggle (bell icon in the admin topbar) — subscribes/
// unsubscribes this browser and tells the server about it via /push/*.
(function () {
  var toggle = document.getElementById("pushToggle");
  if (!toggle) return;
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    toggle.style.display = "none";
    return;
  }

  function urlBase64ToUint8Array(base64String) {
    var padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    var base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    var rawData = atob(base64);
    var outputArray = new Uint8Array(rawData.length);
    for (var i = 0; i < rawData.length; i++) outputArray[i] = rawData.charCodeAt(i);
    return outputArray;
  }

  function refreshState() {
    navigator.serviceWorker.ready.then(function (reg) {
      return reg.pushManager.getSubscription();
    }).then(function (sub) {
      toggle.classList.toggle("subscribed", !!sub);
      toggle.title = sub ? "Уведомления включены (нажмите, чтобы отключить)" : "Включить уведомления";
    });
  }

  toggle.addEventListener("click", function () {
    navigator.serviceWorker.ready.then(function (reg) {
      reg.pushManager.getSubscription().then(function (existing) {
        if (existing) {
          var endpoint = existing.endpoint;
          existing.unsubscribe().then(function () {
            return fetch("/push/unsubscribe", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({endpoint: endpoint}),
            });
          }).then(refreshState).catch(refreshState);
          return;
        }
        Notification.requestPermission().then(function (permission) {
          if (permission !== "granted") return;
          fetch("/push/vapid-public-key").then(function (r) {
            return r.text();
          }).then(function (key) {
            return reg.pushManager.subscribe({
              userVisibleOnly: true,
              applicationServerKey: urlBase64ToUint8Array(key),
            });
          }).then(function (sub) {
            return fetch("/push/subscribe", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify(sub.toJSON()),
            });
          }).then(refreshState).catch(function (err) {
            alert("Не удалось включить уведомления: " + (err && err.message ? err.message : err));
          });
        });
      });
    });
  });

  refreshState();
})();
