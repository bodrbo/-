// Searchable replacement for long <select data-searchable> lists (projects /
// works in analytics). The native <select> stays in the DOM and keeps the
// value, so forms, inline onchange handlers and ajax submits work unchanged:
// picking an entry sets select.value and fires a bubbling "change" event.
// Selects added later (table fragment reloads, split rows) are picked up by a
// MutationObserver.
(function () {
  var openState = null;

  function norm(s) { return (s || "").toLowerCase().replace(/ё/g, "е"); }

  // Flat model of the select: top-level options and optgroups with options.
  function readModel(select) {
    var model = [];
    Array.prototype.forEach.call(select.children, function (node) {
      if (node.tagName === "OPTGROUP") {
        var opts = Array.prototype.map.call(node.children, function (o) {
          return {value: o.value, label: o.textContent.trim(), disabled: o.disabled};
        });
        model.push({group: node.label, options: opts});
      } else if (node.tagName === "OPTION" && !node.disabled) {
        model.push({options: [{value: node.value, label: node.textContent.trim()}]});
      }
    });
    return model;
  }

  function currentLabel(select) {
    var opt = select.options[select.selectedIndex];
    if (!opt) return "";
    var label = opt.textContent.trim();
    if (opt.parentNode && opt.parentNode.tagName === "OPTGROUP") {
      var g = opt.parentNode.label;
      return opt.value.charAt(0) === "p" ? g : g + " " + label;
    }
    return label;
  }

  function closePopup() {
    if (!openState) return;
    openState.popup.remove();
    openState.button.setAttribute("aria-expanded", "false");
    document.removeEventListener("mousedown", onDocDown, true);
    window.removeEventListener("scroll", onScroll, true);
    window.removeEventListener("resize", closePopup);
    openState = null;
  }
  function onDocDown(e) {
    if (openState && !openState.popup.contains(e.target) && e.target !== openState.button) closePopup();
  }
  function onScroll(e) {
    if (openState && !openState.popup.contains(e.target)) closePopup();
  }

  function choose(select, value) {
    closePopup();
    if (select.value !== value) {
      select.value = value;
      select.dispatchEvent(new Event("change", {bubbles: true}));
    }
    var btn = select._ssButton;
    if (btn) { refresh(select); btn.focus(); }
  }

  function refresh(select) {
    var btn = select._ssButton;
    if (!btn) return;
    var opt = select.options[select.selectedIndex];
    var placeholder = !opt || opt.disabled || !opt.value;
    btn.textContent = currentLabel(select);
    btn.title = btn.textContent;
    btn.classList.toggle("ss-placeholder", placeholder);
  }

  function openPopup(select) {
    closePopup();
    var btn = select._ssButton;
    var model = readModel(select);
    var popup = document.createElement("div");
    popup.className = "ss-popup";
    var input = document.createElement("input");
    input.type = "search";
    input.className = "ss-search";
    input.placeholder = "Поиск проекта или работы…";
    input.setAttribute("autocomplete", "off");
    var list = document.createElement("div");
    list.className = "ss-list";
    list.setAttribute("role", "listbox");
    popup.appendChild(input);
    popup.appendChild(list);
    document.body.appendChild(popup);

    var r = btn.getBoundingClientRect();
    var width = Math.max(r.width, 320);
    var left = Math.min(r.left, window.innerWidth - width - 8);
    popup.style.width = width + "px";
    popup.style.left = Math.max(8, left) + "px";
    var below = window.innerHeight - r.bottom;
    if (below < 260 && r.top > below) {
      popup.style.bottom = (window.innerHeight - r.top + 4) + "px";
      list.style.maxHeight = Math.min(320, r.top - 70) + "px";
    } else {
      popup.style.top = (r.bottom + 4) + "px";
      list.style.maxHeight = Math.min(320, below - 70) + "px";
    }
    btn.setAttribute("aria-expanded", "true");

    var items = [];  // currently rendered option rows
    var active = -1;

    function setActive(i, scroll) {
      if (items[active]) items[active].classList.remove("ss-active");
      active = i;
      if (items[active]) {
        items[active].classList.add("ss-active");
        if (scroll) items[active].scrollIntoView({block: "nearest"});
      }
    }

    function render(query) {
      var words = norm(query).split(/\s+/).filter(Boolean);
      list.textContent = "";
      items = [];
      model.forEach(function (entry) {
        var groupText = norm(entry.group || "");
        var shown = entry.options.filter(function (o) {
          if (!words.length) return true;
          var hay = groupText + " " + norm(o.label);
          return words.every(function (w) { return hay.indexOf(w) !== -1; });
        });
        if (!shown.length) return;
        if (entry.group) {
          var h = document.createElement("div");
          h.className = "ss-group";
          h.textContent = entry.group;
          list.appendChild(h);
        }
        shown.forEach(function (o) {
          var row = document.createElement("div");
          row.className = "ss-option" + (entry.group ? " ss-indent" : "") +
            (o.value === select.value ? " ss-selected" : "");
          row.setAttribute("role", "option");
          row.textContent = o.label;
          row.dataset.value = o.value;
          row.addEventListener("mousedown", function (e) { e.preventDefault(); });
          row.addEventListener("click", function () { choose(select, o.value); });
          row.addEventListener("mousemove", function () { setActive(items.indexOf(row), false); });
          list.appendChild(row);
          items.push(row);
        });
      });
      if (!items.length) {
        var empty = document.createElement("div");
        empty.className = "ss-empty";
        empty.textContent = "Ничего не найдено";
        list.appendChild(empty);
      }
      active = -1;
      // With a query, highlight the first hit so Enter picks it.
      var sel = items.findIndex(function (el) { return el.classList.contains("ss-selected"); });
      setActive(words.length ? (items.length ? 0 : -1) : sel, true);
    }

    input.addEventListener("input", function () { render(input.value); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); if (items.length) setActive((active + 1) % items.length, true); }
      else if (e.key === "ArrowUp") { e.preventDefault(); if (items.length) setActive((active - 1 + items.length) % items.length, true); }
      else if (e.key === "Enter") { e.preventDefault(); if (items[active]) choose(select, items[active].dataset.value); }
      else if (e.key === "Escape") { e.preventDefault(); closePopup(); btn.focus(); }
      else if (e.key === "Tab") { closePopup(); }
    });

    openState = {popup: popup, button: btn};
    document.addEventListener("mousedown", onDocDown, true);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", closePopup);
    render("");
    input.focus();
  }

  function enhance(select) {
    if (select._ssButton || !select.hasAttribute("data-searchable")) return;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = select.className + " ss-btn";
    btn.setAttribute("aria-haspopup", "listbox");
    btn.setAttribute("aria-expanded", "false");
    if (select.getAttribute("aria-label")) btn.setAttribute("aria-label", select.getAttribute("aria-label"));
    select._ssButton = btn;
    select.style.display = "none";
    select.parentNode.insertBefore(btn, select.nextSibling);
    btn.addEventListener("click", function () {
      if (openState && openState.button === btn) closePopup(); else openPopup(select);
    });
    btn.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); openPopup(select); }
    });
    select.addEventListener("change", function () { refresh(select); });
    refresh(select);
  }

  function enhanceAll(root) {
    if (root.nodeType !== 1) return;
    if (root.matches && root.matches("select[data-searchable]")) enhance(root);
    root.querySelectorAll && root.querySelectorAll("select[data-searchable]").forEach(enhance);
  }

  function init() {
    enhanceAll(document.body);
    new MutationObserver(function (muts) {
      muts.forEach(function (m) {
        m.addedNodes.forEach(enhanceAll);
        m.removedNodes.forEach(function (n) {
          if (openState && n.nodeType === 1 && n.contains(openState.button)) closePopup();
        });
      });
    }).observe(document.body, {childList: true, subtree: true});
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
