/* Сохранение в фоне для форм, которые меняют одну вещь (статус): страница не
   перезагружается, новое значение видно сразу, а запрос уходит на сервер сам.
   Запросы идут по очереди; пока хоть один не завершён, страница не даёт уйти
   молча — предупреждение при закрытии вкладки, а переход по ссылке и отправка
   другой формы ждут окончания сохранения. Если сервер не принял изменение,
   показывается ошибка и страница возвращает прежнее значение.

     LiveSave.send(form, {
       body:     FormData вместо данных формы (по умолчанию — сама форма),
       refresh:  "селектор|селектор" — блоки, которые нужно перечитать с сервера,
       onSaved:  function (doc, response, isLast) — ответ сервера (страница, на которую он перенаправил),
       onFailed: function (message) — запрос не удался
     })
     LiveSave.refresh("селектор|селектор", doc)  — подменяет блоки страницы блоками из doc
     LiveSave.reread("селектор|селектор")        — то же, но блоки читаются с сервера заново

   Форма с атрибутом data-live-save отправляется скриптом своей страницы через
   LiveSave.send; остальные формы при несохранённом изменении ждут его конца.

   Доска задач заказа работает по той же схеме, но своим скриптом
   (board-live.js), поэтому этот файл на ней не подключается. */
(function () {
  "use strict";
  if (window.LiveSave || !window.fetch || !window.DOMParser) { return; }

  var pending = 0;
  var chain = Promise.resolve();
  var hideTimer = null;
  var releasing = null;

  var indicator = document.createElement("div");
  indicator.className = "board-save-state";
  indicator.setAttribute("role", "status");
  indicator.setAttribute("aria-live", "polite");
  indicator.hidden = true;
  document.body.appendChild(indicator);

  function setState(kind, text) {
    clearTimeout(hideTimer);
    indicator.hidden = false;
    indicator.className = "board-save-state is-" + kind;
    indicator.textContent = text;
    if (kind === "error") {
      var reload = document.createElement("button");
      reload.type = "button";
      reload.textContent = "Обновить страницу";
      reload.addEventListener("click", function () { window.location.reload(); });
      indicator.appendChild(reload);
      hideTimer = setTimeout(function () { indicator.hidden = true; }, 8000);
    } else if (kind === "saved") {
      hideTimer = setTimeout(function () { indicator.hidden = true; }, 1800);
    }
  }

  function parse(html) { return new DOMParser().parseFromString(html, "text/html"); }

  function refresh(selectors, doc) {
    String(selectors || "").split("|").forEach(function (selector) {
      selector = selector.trim();
      if (!selector) { return; }
      var current = document.querySelector(selector);
      var fresh = doc.querySelector(selector);
      if (current && fresh) { current.replaceWith(fresh); }
    });
  }

  // blocks that depend on the change are read from the page itself, not from
  // the page the server redirected to (which may carry other filters)
  function reread(selectors) {
    return fetch(window.location.pathname + window.location.search, { credentials: "same-origin" })
      .then(function (response) { return response.ok ? response.text() : ""; })
      .then(function (html) { if (html) { refresh(selectors, parse(html)); } })
      .catch(function () { /* the next change reads them again */ });
  }

  function expired(response) {
    try { return response.redirected && /login/i.test(new URL(response.url).pathname); } catch (error) { return false; }
  }

  function send(form, options) {
    options = options || {};
    var body = options.body || new FormData(form);
    pending += 1;
    setState("saving", "Сохраняем…");
    chain = chain.then(function () {
      return fetch(form.action, {
        method: "POST", body: body, credentials: "same-origin", redirect: "follow",
        headers: { "X-Requested-With": "XMLHttpRequest" }
      });
    }).then(function (response) {
      if (!response.ok || expired(response)) { throw new Error(expired(response) ? "expired" : "failed"); }
      return response.text().then(function (html) { return { response: response, doc: parse(html) }; });
    }).then(function (result) {
      var isLast = pending <= 1;
      if (options.onSaved) {
        try { options.onSaved(result.doc, result.response, isLast); } catch (error) { if (window.console) { console.error(error); } }
      }
      var done = options.refresh && isLast ? reread(options.refresh) : null;
      setState("saved", "Сохранено");
      return done;
    }).catch(function (error) {
      var message = error && error.message === "expired"
        ? "Сессия закончилась — изменение не сохранено. Войдите заново."
        : "Не удалось сохранить изменение.";
      if (options.onFailed) { options.onFailed(message); }
      setState("error", message);
    }).then(function () { pending -= 1; });
    return chain;
  }

  // ---- not leaving with something unsent
  window.addEventListener("beforeunload", function (event) {
    if (pending > 0) { event.preventDefault(); event.returnValue = ""; }
  });

  document.addEventListener("click", function (event) {
    if (!pending || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) { return; }
    var link = event.target.closest && event.target.closest("a[href]");
    if (!link || link.target === "_blank" || link.hasAttribute("download")) { return; }
    var href = link.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#" || link.origin !== window.location.origin) { return; }
    event.preventDefault();
    event.stopPropagation();
    setState("saving", "Дожидаемся сохранения…");
    chain.then(function () { window.location.href = link.href; });
  }, true);

  // another form sent while a change is still on its way: wait, then send it
  // the ordinary way (all its own handlers and confirmations run as usual)
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!pending || event.defaultPrevented || releasing === form || !(form instanceof HTMLFormElement)) { return; }
    if (form.target && form.target !== "_self") { return; }
    // a form marked data-live-save is sent in the background by its own page script
    if (form.hasAttribute("data-live-save")) { return; }
    event.preventDefault();
    event.stopImmediatePropagation();
    var submitter = event.submitter;
    setState("saving", "Дожидаемся сохранения…");
    chain.then(function () {
      releasing = form;
      try {
        if (form.requestSubmit) { if (submitter) { form.requestSubmit(submitter); } else { form.requestSubmit(); } } else { form.submit(); }
      } finally { releasing = null; }
    });
  }, true);

  window.LiveSave = { send: send, refresh: refresh, reread: reread, pending: function () { return pending; } };
})();
