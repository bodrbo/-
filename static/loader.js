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
