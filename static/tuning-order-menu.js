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
  document.addEventListener('click', function (event) {
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
