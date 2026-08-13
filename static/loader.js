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

// Instant client-side table search — an <input data-filter-table="#id">
// hides/shows the referenced table's <tbody> rows as you type, matching
// against each row's full text (name, article, address, whatever columns
// that table has), no server round-trip. Used on the supply catalog and
// warehouse pages, but written generically so any table can opt in.
(function () {
  var inputs = document.querySelectorAll("input[data-filter-table]");
  if (!inputs.length) return;

  inputs.forEach(function (input) {
    var table = document.querySelector(input.getAttribute("data-filter-table"));
    var tbody = table && table.querySelector("tbody");
    if (!tbody) return;
    var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
    var noResults = document.querySelector(input.getAttribute("data-filter-table") + "-no-results");
    var timer = null;

    function apply() {
      var q = input.value.trim().toLowerCase();
      var visible = 0;
      rows.forEach(function (row) {
        var match = !q || row.textContent.toLowerCase().indexOf(q) !== -1;
        row.classList.toggle("hidden", !match);
        if (match) visible += 1;
      });
      if (noResults) noResults.classList.toggle("hidden", visible !== 0 || !q);
    }

    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(apply, 120);
    });
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
