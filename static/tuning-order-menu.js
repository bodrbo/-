(function () {
  'use strict';
  var active = null;
  function items(menu) { return Array.prototype.slice.call(menu.querySelectorAll('[role="menuitem"]')); }
  function close(restoreFocus) {
    if (!active) return;
    var state = active;
    active = null;
    state.menu.hidden = true;
    state.trigger.setAttribute('aria-expanded', 'false');
    state.parent.appendChild(state.menu);
    if (restoreFocus) state.trigger.focus({preventScroll: true});
  }
  function open(trigger, last) {
    var same = active && active.trigger === trigger;
    close(false);
    if (same) return;
    var menu = document.getElementById(trigger.getAttribute('aria-controls'));
    if (!menu) return;
    active = {trigger: trigger, menu: menu, parent: menu.parentElement};
    // Outside the scroll container so even the last row's menu is not clipped.
    document.body.appendChild(menu);
    menu.style.visibility = 'hidden';
    menu.hidden = false;
    var rect = trigger.getBoundingClientRect();
    var width = menu.offsetWidth;
    var height = menu.offsetHeight;
    var left = Math.max(8, Math.min(rect.right - width, document.documentElement.clientWidth - width - 8));
    var top = rect.bottom + 6;
    if (top + height > window.innerHeight - 8) top = rect.top - height - 6;
    menu.style.left = left + 'px';
    menu.style.top = Math.max(8, top) + 'px';
    menu.style.visibility = '';
    trigger.setAttribute('aria-expanded', 'true');
    var choices = items(menu);
    choices[last ? choices.length - 1 : 0].focus({preventScroll: true});
  }
  var transferDialog = document.getElementById('tuning-transfer-dialog');
  var linkDialog = document.getElementById('tuning-link-dialog');
  var linkForm = document.getElementById('tuning-link-form');
  var search = document.getElementById('tuning-link-search');
  var select = document.getElementById('tuning-link-select');
  var save = document.getElementById('tuning-link-submit');
  var status = document.getElementById('tuning-link-status');
  var linkUrl = '';
  var initialized = false;
  var loadSequence = 0;
  var searchTimer;
  var dialogOpener;
  async function loadOrders() {
    var sequence = ++loadSequence;
    var chosen = initialized ? select.value : null;
    select.disabled = true;
    save.disabled = true;
    status.textContent = 'Загружаем заказы…';
    try {
      var url = new URL(linkUrl, window.location.origin);
      url.searchParams.set('q', search.value);
      var response = await fetch(url, {headers: {'Accept': 'application/json'}});
      if (!response.ok) throw new Error('load');
      var data = await response.json();
      if (sequence !== loadSequence) return;
      select.replaceChildren();
      var empty = new Option(data.current_id ? 'Без привязки (снять связь)' : 'Выберите дилерский заказ…', '');
      empty.disabled = !data.current_id;
      select.add(empty);
      data.orders.forEach(function (order) { select.add(new Option(order.label, String(order.id))); });
      select.value = chosen === null ? String(data.current_id || '') : chosen;
      if (select.selectedIndex < 0) select.selectedIndex = 0;
      select.required = !data.current_id;
      initialized = true;
      select.disabled = false;
      save.disabled = data.orders.length === 0 && !data.current_id;
      status.textContent = data.orders.length ? 'Сначала показаны заказы этого клиента. Можно изменить или снять существующую связь.' : 'Заказы не найдены. Измените поиск.';
    } catch (_) {
      if (sequence === loadSequence) status.textContent = 'Не удалось загрузить заказы. Попробуйте изменить поиск или открыть окно заново.';
    }
  }
  if (search) search.addEventListener('input', function () {
    window.clearTimeout(searchTimer);
    // Invalidate the previous request before the debounce elapses.
    ++loadSequence;
    save.disabled = true;
    searchTimer = window.setTimeout(loadOrders, 250);
  });
  [transferDialog, linkDialog].forEach(function (dialog) {
    if (!dialog) return;
    dialog.querySelector('[data-order-dialog-close]').addEventListener('click', function () { dialog.close(); });
    dialog.addEventListener('click', function (event) { if (event.target === dialog) dialog.close(); });
    dialog.addEventListener('close', function () {
      ++loadSequence;
      window.clearTimeout(searchTimer);
      if (dialogOpener) dialogOpener.focus({preventScroll: true});
    });
  });
  document.addEventListener('click', function (event) {
    var action = event.target.closest('[data-order-dealer-action]');
    if (action) {
      dialogOpener = active ? active.trigger : null;
      close(false);
      if (action.dataset.orderDealerAction === 'transfer') {
        transferDialog.querySelector('[name="order_id"]').value = action.dataset.orderId;
        transferDialog.querySelector('[name="bulk_target"]').value = '';
        document.getElementById('tuning-transfer-title').textContent = 'Перенести заказ №' + action.dataset.orderId;
        transferDialog.showModal();
      } else {
        linkUrl = action.dataset.linkUrl;
        linkForm.action = linkUrl;
        initialized = false;
        search.value = '';
        select.replaceChildren();
        document.getElementById('tuning-link-title').textContent = 'Привязать заказ №' + action.dataset.orderId;
        linkDialog.showModal();
        loadOrders();
      }
      return;
    }
    var trigger = event.target.closest('[data-order-menu-trigger]');
    if (trigger) { open(trigger, false); return; }
    if (active && !active.menu.contains(event.target)) close(false);
  });
  document.addEventListener('keydown', function (event) {
    var trigger = event.target.closest('[data-order-menu-trigger]');
    if (trigger && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      event.preventDefault(); open(trigger, event.key === 'ArrowUp'); return;
    }
    if (!active) return;
    if (event.key === 'Escape') { event.preventDefault(); close(true); return; }
    if (event.key === 'Tab') { close(true); return; }
    var choices = items(active.menu);
    var index = choices.indexOf(document.activeElement);
    if (index < 0) return;
    var next;
    if (event.key === 'ArrowDown') next = (index + 1) % choices.length;
    else if (event.key === 'ArrowUp') next = (index + choices.length - 1) % choices.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = choices.length - 1;
    if (next !== undefined) { event.preventDefault(); choices[next].focus({preventScroll: true}); }
  });
  window.addEventListener('resize', function () { close(false); });
  document.addEventListener('scroll', function () { close(false); }, true);
})();
