/* A clickable status (see templates/_status_picker.html): the badge opens a
   list of statuses; picking another one shows it at once and saves it in the
   background (static/live-save.js) — the page is not reloaded. Where that
   script is absent (the order task board has its own, board-live.js) the form
   is simply submitted. The list is positioned with position: fixed so it is
   never clipped by a scrolling table, and it works with the mouse, a finger
   and the keyboard (Enter / Space / ↓ open it, ↑ ↓ move, Enter picks, Esc
   closes). */
(function () {
  "use strict";
  var openPicker = null;

  function close() {
    if (!openPicker) { return; }
    var menu = openPicker.querySelector("[data-status-menu]");
    var trigger = openPicker.querySelector("[data-status-trigger]");
    menu.hidden = true;
    menu.style.cssText = "";
    trigger.setAttribute("aria-expanded", "false");
    openPicker = null;
  }

  function options(picker) {
    return Array.prototype.filter.call(
      picker.querySelectorAll(".status-picker-option"),
      function (option) { return !option.disabled; }
    );
  }

  function place(picker) {
    var trigger = picker.querySelector("[data-status-trigger]");
    var menu = picker.querySelector("[data-status-menu]");
    var box = trigger.getBoundingClientRect();
    menu.style.position = "fixed";
    menu.style.visibility = "hidden";
    menu.hidden = false;
    var width = menu.offsetWidth;
    var height = menu.offsetHeight;
    var left = Math.max(8, Math.min(box.left, window.innerWidth - width - 8));
    var top = box.bottom + 6;
    if (top + height > window.innerHeight - 8 && box.top - height - 6 > 8) {
      top = box.top - height - 6;
    }
    menu.style.left = left + "px";
    menu.style.top = Math.max(8, top) + "px";
    menu.style.visibility = "";
  }

  function open(picker) {
    if (openPicker === picker) { return; }
    close();
    place(picker);
    picker.querySelector("[data-status-trigger]").setAttribute("aria-expanded", "true");
    openPicker = picker;
    var current = picker.querySelector(".status-picker-option.is-current") || options(picker)[0];
    if (current) { focusOption(current); }
  }

  // The menu is position: fixed, so focusing an option must never scroll the
  // page (a scroll closes the menu).
  function focusOption(option) {
    try { option.focus({ preventScroll: true }); } catch (error) { option.focus(); }
  }

  // shows a status on the badge (the hidden field holds what will be saved)
  function show(picker, value, text) {
    var form = picker.closest("[data-status-picker-form]");
    var input = form.querySelector("[data-status-value]");
    var trigger = picker.querySelector("[data-status-trigger]");
    var prefix = trigger.getAttribute("data-status-prefix") || "status-";
    trigger.classList.remove(prefix + input.value);
    trigger.classList.add(prefix + value);
    input.value = value;
    var label = picker.querySelector(".status-picker-label");
    if (label && text) { label.textContent = text; }
    picker.querySelectorAll(".status-picker-option").forEach(function (option) {
      var current = option.getAttribute("data-value") === value;
      option.classList.toggle("is-current", current);
      option.setAttribute("aria-selected", current ? "true" : "false");
    });
  }

  function optionLabel(picker, value) {
    var option = picker.querySelector('.status-picker-option[data-value="' + value + '"]');
    return option ? option.textContent.replace(/\s+/g, " ").trim() : "";
  }

  function save(picker, form, previous) {
    var seq = (form._statusSeq || 0) + 1;
    form._statusSeq = seq;
    picker.classList.add("is-saving");
    window.LiveSave.send(form, {
      refresh: form.getAttribute("data-live-refresh"),
      onSaved: function (doc) {
        if (form._statusSeq !== seq) { return; }
        picker.classList.remove("is-saving");
        // what the server really stored wins over what was clicked
        var action = form.getAttribute("action");
        var stored = Array.prototype.filter.call(doc.querySelectorAll("[data-status-picker-form]"), function (other) {
          return other.getAttribute("action") === action;
        })[0];
        var input = form.querySelector("[data-status-value]");
        var value = stored && stored.querySelector("[data-status-value]").value;
        if (value && value !== input.value) { show(picker, value, optionLabel(picker, value)); }
      },
      onFailed: function () {
        if (form._statusSeq !== seq) { return; }
        picker.classList.remove("is-saving");
        show(picker, previous.value, previous.label);
      }
    });
  }

  function choose(picker, option) {
    var form = picker.closest("[data-status-picker-form]");
    var input = form.querySelector("[data-status-value]");
    var value = option.getAttribute("data-value");
    var label = picker.querySelector(".status-picker-label");
    var previous = { value: input.value, label: label ? label.textContent : "" };
    close();
    if (value === previous.value) { return; }
    // show the new status at once
    show(picker, value, option.textContent.replace(/\s+/g, " ").trim());
    if (window.LiveSave && !form.closest(".tuning-board-page")) {
      save(picker, form, previous);
      return;
    }
    picker.classList.add("is-saving");
    if (form.requestSubmit) { form.requestSubmit(); } else { form.submit(); }
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-status-trigger]");
    if (trigger) {
      event.preventDefault();
      event.stopPropagation();
      var picker = trigger.closest("[data-status-picker]");
      if (openPicker === picker) { close(); } else { open(picker); }
      return;
    }
    var option = event.target.closest(".status-picker-option");
    if (option) {
      event.preventDefault();
      event.stopPropagation();
      choose(option.closest("[data-status-picker]"), option);
      return;
    }
    if (openPicker && !openPicker.contains(event.target)) { close(); }
  });

  document.addEventListener("keydown", function (event) {
    var trigger = event.target.closest && event.target.closest("[data-status-trigger]");
    if (trigger && !openPicker && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault();
      open(trigger.closest("[data-status-picker]"));
      return;
    }
    if (!openPicker) { return; }
    var list = options(openPicker);
    var index = list.indexOf(document.activeElement);
    if (event.key === "Escape") {
      event.preventDefault();
      var back = openPicker.querySelector("[data-status-trigger]");
      close();
      back.focus();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      if (list.length) { focusOption(list[(index + 1) % list.length]); }
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (list.length) { focusOption(list[(index - 1 + list.length) % list.length]); }
    } else if (event.key === "Tab") {
      close();
    }
  });

  window.addEventListener("scroll", function (event) {
    // the list scrolling inside itself is not the page moving away
    if (openPicker && event.target && event.target.nodeType === 1 && openPicker.contains(event.target)) { return; }
    close();
  }, true);
  window.addEventListener("resize", close);
})();
