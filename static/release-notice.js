(function () {
  'use strict';
  var dialog = document.getElementById('release-notice');
  var data = document.getElementById('release-notice-config');
  if (!dialog || !data || typeof dialog.showModal !== 'function') return;
  var config = JSON.parse(data.textContent);
  var button = document.getElementById('release-notice-confirm');
  var error = document.getElementById('release-notice-error');
  var previousFocus;
  var overflow;
  var busy = false;
  function locallySeen() {
    try { return localStorage.getItem(config.storageKey) === '1'; } catch (_) { return false; }
  }
  function close() {
    if (!dialog.open) return;
    dialog.close();
    document.documentElement.style.overflow = overflow[0];
    document.body.style.overflow = overflow[1];
    if (previousFocus && previousFocus.isConnected) previousFocus.focus({preventScroll: true});
  }
  function show() {
    if (locallySeen() || dialog.open) return;
    // Avoid stacking this announcement over an existing native modal.
    if (document.querySelector('dialog[open]')) {
      window.setTimeout(show, 500);
      return;
    }
    previousFocus = document.activeElement;
    overflow = [document.documentElement.style.overflow, document.body.style.overflow];
    document.documentElement.style.overflow = 'hidden';
    document.body.style.overflow = 'hidden';
    dialog.showModal();
    button.focus({preventScroll: true});
  }
  async function acknowledge() {
    if (busy) return;
    busy = true;
    button.disabled = true;
    error.hidden = true;
    try {
      var response = await fetch(config.endpoint, {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token: config.token})
      });
      if (!response.ok) throw new Error('save');
      try { localStorage.setItem(config.storageKey, '1'); } catch (_) {}
      close();
    } catch (_) {
      error.textContent = 'Не удалось сохранить. Проверьте соединение и нажмите «Понятно» ещё раз.';
      error.hidden = false;
    } finally {
      busy = false;
      button.disabled = false;
    }
  }
  button.addEventListener('click', acknowledge);
  dialog.addEventListener('cancel', function (event) { event.preventDefault(); acknowledge(); });
  window.addEventListener('storage', function (event) {
    if (event.key === config.storageKey && event.newValue === '1') close();
  });
  if (document.readyState === 'complete') show();
  else window.addEventListener('load', show, {once: true});
})();
