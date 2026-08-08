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
