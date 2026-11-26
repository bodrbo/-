(function () {
  'use strict';
  var data = document.getElementById('navigation-preferences-data');
  if (!data) return;
  var config = JSON.parse(data.textContent);
  var menu = document.createElement('div');
  menu.className = 'start-page-menu';
  menu.hidden = true;
  menu.setAttribute('role', 'menu');
  document.body.appendChild(menu);
  var trigger;
  var busy = false;
  var status = document.createElement('div');
  status.className = 'start-page-notice';
  status.setAttribute('role', 'status');
  status.hidden = true;
  document.body.appendChild(status);

  function close() { menu.hidden = true; }
  function notify(text, isError) {
    status.textContent = text;
    status.hidden = false;
    window.clearTimeout(notify.timer);
    notify.timer = window.setTimeout(function () { status.hidden = true; }, isError ? 5000 : 1000);
  }
  function save(token) {
    if (busy) return;
    busy = true;
    menu.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    fetch(config.endpoint, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: token })
    }).then(function (response) {
      return response.json().then(function (result) {
        if (!response.ok) throw new Error(result.error || 'Не удалось сохранить настройку.');
        config.current = result.url;
        notify(result.url ? 'Стартовый экран сохранён. Он откроется при следующем входе.' : 'Стандартный стартовый экран восстановлен.');
        close();
        if (trigger) trigger.focus();
      });
    }).catch(function (error) { notify(error.message || 'Проверьте соединение и повторите.', true); })
      .finally(function () {
        busy = false;
        menu.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
      });
  }
  function addButton(text, token) {
    var button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('role', 'menuitem');
    button.textContent = text;
    button.addEventListener('click', function () { save(token); });
    menu.appendChild(button);
  }
  document.addEventListener('contextmenu', function (event) {
    var link = event.target.closest('a');
    if (!link || !link.closest('nav.main-nav, nav.sub-tabs, nav.team-mobile-nav')) return;
    var href = link.getAttribute('href');
    if (!config.links[href]) return;
    event.preventDefault();
    trigger = link;
    menu.replaceChildren();
    var label = document.createElement('div');
    label.className = 'start-page-menu-title';
    label.textContent = link.textContent.trim().replace(/\s+/g, ' ');
    menu.appendChild(label);
    addButton(config.current === href ? '✓ Выбран стартовым экраном' : 'Открывать по умолчанию', config.links[href]);
    if (config.current) addButton('Сбросить стартовый экран', config.reset);
    menu.hidden = false;
    var box = menu.getBoundingClientRect();
    menu.style.left = Math.max(8, Math.min(event.clientX, window.innerWidth - box.width - 8)) + 'px';
    menu.style.top = Math.max(8, Math.min(event.clientY, window.innerHeight - box.height - 8)) + 'px';
    menu.querySelector('button').focus();
  });
  document.addEventListener('pointerdown', function (event) { if (!menu.contains(event.target)) close(); });
  document.addEventListener('keydown', function (event) {
    if (menu.hidden) return;
    if (event.key === 'Escape') { close(); if (trigger) trigger.focus(); }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      var buttons = Array.from(menu.querySelectorAll('button'));
      var i = buttons.indexOf(document.activeElement);
      buttons[(i + (event.key === 'ArrowDown' ? 1 : buttons.length - 1)) % buttons.length].focus();
    }
  });
  window.addEventListener('resize', close);
  window.addEventListener('scroll', close, true);
})();
