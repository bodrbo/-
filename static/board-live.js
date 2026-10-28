/* Доска задач заказа: изменения не перезагружают страницу.
   Любая форма на доске отправляется в фоне (смена статуса, исполнителя, срока,
   ставки, комментарий, напоминание, поручение, отзыв задачи); ответ сервера
   подменяет только затронутую колонку работы, сводку заказа и сообщения, так
   что прокрутка, открытые блоки и несохранённый ввод в других колонках
   остаются на месте. Запросы идут по очереди; пока хоть один не завершён,
   страница не даёт уйти молча (предупреждение и ожидание перед переходом по
   ссылке). Если запрос не удался, показывается ошибка, а доска перечитывается
   с сервера, чтобы на экране было то, что реально сохранено. */
(function () {
  "use strict";
  var page = document.querySelector(".tuning-board-page");
  if (!page || !window.fetch || !window.DOMParser) { return; }

  var pending = 0;
  var failed = false;
  var chain = Promise.resolve();
  var hideTimer = null;

  // ---- the save indicator
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
    }
    if (kind === "saved") {
      hideTimer = setTimeout(function () { indicator.hidden = true; }, 1800);
    }
  }

  // ---- unsaved input in a column survives the replacement of that column
  page.addEventListener("input", markDirty);
  page.addEventListener("change", markDirty);
  function markDirty(event) {
    var column = event.target.closest && event.target.closest(".board-column");
    if (column) { column.setAttribute("data-dirty", "1"); }
  }

  function fieldChanged(field) {
    if (field.tagName === "SELECT") {
      return Array.prototype.some.call(field.options, function (option) { return option.selected !== option.defaultSelected; });
    }
    if (field.type === "checkbox" || field.type === "radio") { return field.checked !== field.defaultChecked; }
    return field.value !== field.defaultValue;
  }

  function collectDirty(column, exceptForm) {
    var saved = [];
    column.querySelectorAll("form").forEach(function (form) {
      if (form === exceptForm) { return; }
      form.querySelectorAll("input, textarea, select").forEach(function (field, index) {
        if (field.type === "hidden" || field.type === "submit" || field.type === "button") { return; }
        if (fieldChanged(field)) {
          saved.push({ action: form.getAttribute("action"), name: field.name, index: index, value: field.value, checked: field.checked });
        }
      });
    });
    return saved;
  }

  function restoreDirty(column, saved) {
    saved.forEach(function (entry) {
      Array.prototype.forEach.call(column.querySelectorAll("form"), function (form) {
        if (form.getAttribute("action") !== entry.action) { return; }
        var field = form.querySelectorAll("input, textarea, select")[entry.index];
        if (!field || field.name !== entry.name) { return; }
        if (field.type === "checkbox" || field.type === "radio") { field.checked = entry.checked; } else { field.value = entry.value; }
      });
    });
    if (saved.length) { column.setAttribute("data-dirty", "1"); }
  }

  function openDetails(column) {
    return Array.prototype.map.call(column.querySelectorAll("details"), function (d) { return d.open; });
  }
  function restoreDetails(column, states) {
    Array.prototype.forEach.call(column.querySelectorAll("details"), function (d, i) {
      if (states[i] !== undefined && (states[i] || !d.hasAttribute("open"))) { d.open = states[i]; }
    });
  }

  function replaceColumn(oldColumn, newColumn, exceptForm) {
    var saved = collectDirty(oldColumn, exceptForm);
    var details = openDetails(oldColumn);
    oldColumn.replaceWith(newColumn);
    restoreDetails(newColumn, details);
    restoreDirty(newColumn, saved);
  }

  // ---- applying the server's answer
  function swap(selector, doc) {
    var current = page.querySelector(selector);
    var fresh = doc.querySelector(selector);
    if (current && fresh) { current.replaceWith(fresh); }
    else if (current && !fresh) { current.innerHTML = ""; }
  }

  function apply(doc, form) {
    swap("#board-alerts", doc);
    swap(".board-order-summary", doc);
    var targetId = null;
    var own = form && form.closest(".board-column");
    if (own) { targetId = own.getAttribute("data-item-id"); }
    else if (form) {
      var match = /\/item\/(\d+)\/assign/.exec(form.getAttribute("action") || "");
      if (match) { targetId = match[1]; }
    }
    var fresh = {};
    doc.querySelectorAll(".board-column").forEach(function (column) { fresh[column.getAttribute("data-item-id")] = column; });
    page.querySelectorAll(".board-column").forEach(function (column) {
      var id = column.getAttribute("data-item-id");
      var next = fresh[id];
      if (!next) { return; }
      if (id === targetId || !column.getAttribute("data-dirty")) { replaceColumn(column, next, form); }
    });
  }

  function firstError(doc) {
    // only a refusal of the form itself (not a warning that follows a success)
    var alert = doc.querySelector("#board-alerts [data-form-error]");
    return alert ? alert.textContent.replace(/\s+/g, " ").trim() : "";
  }

  function showAssignError(form, text) {
    var box = form.querySelector(".assign-form-error");
    if (!box) {
      box = document.createElement("div");
      box.className = "alert assign-form-error";
      box.setAttribute("role", "alert");
      form.insertBefore(box, form.firstChild);
    }
    box.textContent = text;
    box.hidden = !text;
  }

  function reconcile() {
    return fetch(window.location.pathname + window.location.search, { credentials: "same-origin" })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        if (!doc.querySelector(".tuning-board-page")) { throw new Error("not the board"); }
        swap("#board-alerts", doc);
        swap(".board-order-summary", doc);
        var fresh = {};
        doc.querySelectorAll(".board-column").forEach(function (c) { fresh[c.getAttribute("data-item-id")] = c; });
        page.querySelectorAll(".board-column").forEach(function (column) {
          var next = fresh[column.getAttribute("data-item-id")];
          if (next) { column.replaceWith(next); }
        });
        failed = false;
      });
  }

  function send(form, submitter) {
    var body;
    try { body = new FormData(form, submitter || undefined); } catch (error) { body = new FormData(form); }
    if (submitter && submitter.name && !body.has(submitter.name)) { body.append(submitter.name, submitter.value); }
    var isAssign = form.id === "assign-form";
    pending += 1;
    setState("saving", "Сохраняем…");
    chain = chain.then(function () {
      return fetch(form.action, {
        method: "POST", body: body, credentials: "same-origin", redirect: "follow",
        headers: { "X-Requested-With": "XMLHttpRequest" }
      });
    }).then(function (response) {
      return response.text().then(function (html) { return { response: response, html: html }; });
    }).then(function (result) {
      var doc = new DOMParser().parseFromString(result.html, "text/html");
      if (!result.response.ok || !doc.querySelector(".tuning-board-page")) {
        // not the board (another page, a login screen, an error): follow it the ordinary way
        if (result.response.ok && result.response.url) { window.location.href = result.response.url; return; }
        throw new Error("save failed");
      }
      var error = isAssign ? firstError(doc) : "";
      apply(doc, form);
      if (isAssign) {
        if (error) { showAssignError(form, error); } else { showAssignError(form, ""); if (window.closeAssignModal) { window.closeAssignModal(); } }
      }
      setState("saved", "Сохранено");
    }).catch(function () {
      failed = true;
      document.querySelectorAll(".status-picker.is-saving").forEach(function (el) { el.classList.remove("is-saving"); });
      setState("error", "Не удалось сохранить изменение.");
      return reconcile().then(function () {
        setState("error", "Не удалось сохранить изменение — доска обновлена из базы.");
        hideTimer = setTimeout(function () { indicator.hidden = true; }, 7000);
      }).catch(function () { /* stays an error: the page may be stale */ });
    }).then(function () { pending -= 1; });
  }

  // Capture phase: this runs before the page-wide loader overlay (which would
  // cover the screen) and before the form's own inline onsubmit — that one is
  // called by hand so a "Вы уверены?" confirmation still works.
  page.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || event.defaultPrevented) { return; }
    if ((form.getAttribute("method") || "get").toLowerCase() !== "post" || form.hasAttribute("data-no-live") || form.target) { return; }
    event.preventDefault();
    event.stopPropagation();
    if (typeof form.onsubmit === "function" && form.onsubmit.call(form, event) === false) { return; }
    send(form, event.submitter);
  }, true);

  // ---- not leaving with something unsent
  window.addEventListener("beforeunload", function (event) {
    if (pending > 0 || failed) { event.preventDefault(); event.returnValue = ""; }
  });
  document.addEventListener("click", function (event) {
    if (!pending || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) { return; }
    var link = event.target.closest("a[href]");
    if (!link || link.target === "_blank" || link.hasAttribute("download")) { return; }
    var href = link.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#" || link.origin !== window.location.origin) { return; }
    event.preventDefault();
    event.stopPropagation();
    setState("saving", "Дожидаемся сохранения…");
    chain.then(function () { window.location.href = link.href; });
  }, true);
})();
