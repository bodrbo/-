(function () {
  'use strict';
  var configElement = document.getElementById('tuning-terms-config');
  if (!configElement) return;
  var config = JSON.parse(configElement.textContent);
  var current = config.terms;
  var dialog = document.getElementById('tuning-terms-dialog');
  var opener = document.getElementById('tuning-terms-open');
  var type = document.getElementById('tuning-terms-type');
  var percent = document.getElementById('tuning-terms-percent');
  var rows = document.getElementById('tuning-terms-rows');
  var error = document.getElementById('tuning-terms-error');
  var save = document.getElementById('tuning-terms-save');
  var clear = document.getElementById('tuning-terms-clear');
  var busy = false;
  function number(raw) { return Number(String(raw).replace(/[\s\u00a0]/g, '').replace(',', '.')); }
  function money(value) { return new Intl.NumberFormat('ru-RU', {style: 'currency', currency: 'RUB'}).format(value); }
  function updateTotal() {
    var total = 0;
    rows.querySelectorAll('[data-amount]').forEach(function (input) {
      var value = number(input.value);
      if (Number.isFinite(value)) total += Math.round(value * 100);
    });
    var difference = Math.round(number(config.orderTotal) * 100) - total;
    document.getElementById('tuning-terms-total').textContent = 'Итого по графику: ' + money(total / 100) +
      (difference ? '. Сумма заказа: ' + money(number(config.orderTotal)) + '. Разница: ' + money(difference / 100) + '.' : '');
  }
  function addRow(payment) {
    if (rows.children.length >= 100) return;
    var row = document.createElement('div');
    row.className = 'tuning-terms-row';
    var date = document.createElement('input');
    date.type = 'date'; date.setAttribute('data-date', ''); date.setAttribute('aria-label', 'Дата платежа');
    date.value = payment ? payment.date : '';
    var amount = document.createElement('input');
    amount.type = 'text'; amount.inputMode = 'decimal'; amount.setAttribute('data-amount', '');
    amount.setAttribute('aria-label', 'Сумма платежа, ₽'); amount.placeholder = '0,00';
    amount.value = payment ? payment.amount.replace('.', ',') : '';
    var remove = document.createElement('button');
    remove.type = 'button'; remove.className = 'tuning-terms-remove'; remove.textContent = '×';
    remove.setAttribute('aria-label', 'Удалить платёж');
    remove.addEventListener('click', function () {
      if (busy) return;
      row.remove(); updateTotal();
    });
    date.required = amount.required = type.value === 'schedule';
    row.append(date, amount, remove); rows.appendChild(row); updateTotal();
  }
  function showMode() {
    document.getElementById('tuning-terms-prepayment').hidden = type.value !== 'prepayment';
    document.getElementById('tuning-terms-schedule').hidden = type.value !== 'schedule';
    percent.required = type.value === 'prepayment';
    rows.querySelectorAll('input').forEach(function (input) { input.required = type.value === 'schedule'; });
    if (type.value === 'schedule' && !rows.children.length) addRow();
  }
  function open() {
    type.value = current ? current.type : 'prepayment';
    percent.value = current && current.type === 'prepayment' ? current.percent.replace('.', ',') : '';
    rows.replaceChildren();
    if (current && current.type === 'schedule') current.payments.forEach(addRow);
    else addRow();
    clear.hidden = !current;
    error.hidden = true;
    showMode(); dialog.showModal();
  }
  async function submit(payload) {
    if (busy) return;
    busy = true; save.disabled = true; clear.disabled = true; error.hidden = true;
    try {
      var response = await fetch(config.endpoint, {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      var result;
      try { result = await response.json(); } catch (_) { throw new Error('Не удалось сохранить. Обновите страницу и повторите.'); }
      if (!response.ok) throw new Error(result.error || 'Не удалось сохранить условия.');
      current = result.terms; config.terms = current;
      configElement.textContent = JSON.stringify(config);
      opener.textContent = result.label;
      dialog.close(); opener.focus({preventScroll: true});
    } catch (failure) {
      error.textContent = failure.message || 'Не удалось сохранить условия.'; error.hidden = false;
    } finally { busy = false; save.disabled = false; clear.disabled = false; }
  }
  opener.addEventListener('click', open);
  document.getElementById('tuning-terms-close').addEventListener('click', function () { if (!busy) dialog.close(); });
  dialog.addEventListener('cancel', function (event) { if (busy) event.preventDefault(); });
  type.addEventListener('change', showMode);
  rows.addEventListener('input', updateTotal);
  document.getElementById('tuning-terms-add').addEventListener('click', function () { if (!busy) { addRow(); showMode(); } });
  clear.addEventListener('click', function () { submit({type: 'none'}); });
  document.getElementById('tuning-terms-form').addEventListener('submit', function (event) {
    event.preventDefault();
    var payload = type.value === 'prepayment' ? {type: 'prepayment', percent: percent.value} : {
      type: 'schedule', payments: Array.prototype.map.call(rows.children, function (row) {
        return {date: row.querySelector('[data-date]').value, amount: row.querySelector('[data-amount]').value};
      })
    };
    submit(payload);
  });
})();
