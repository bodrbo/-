/* A clickable status (see templates/_status_picker.html): the badge opens a
   list of statuses; picking another one sets the hidden field and submits the
   form. The list is positioned with position: fixed so it is never clipped by
   a scrolling table, and it works with the mouse, a finger and the keyboard
   (Enter / Space / ↓ open it, ↑ ↓ move, Enter picks, Esc closes). */
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

  function choose(picker, option) {
    var form = picker.closest("[data-status-picker-form]");
    var input = form.querySelector("[data-status-value]");
    var value = option.getAttribute("data-value");
    var current = input.value;
    close();
    if (value === current) { return; }
    input.value = value;
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
