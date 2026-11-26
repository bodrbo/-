(function () {
  var dialog = document.getElementById('dealerOptionDialog');
  if (!dialog) return;
  document.addEventListener('click', function (event) {
    var trigger = event.target.closest('[data-dealer-option]');
    if (!trigger) return;
    var option;
    try { option = JSON.parse(trigger.dataset.dealerOption); } catch (error) { return; }
    document.getElementById('dealerOptionTitle').textContent = option.name || 'Доп. опция';
    dialog.querySelector('[data-option-photo]').src = dialog.dataset.photoUrl.replace('__PHOTO__', encodeURIComponent(option.photo_filename));
    dialog.querySelector('[data-option-boats]').textContent = option.boat_name || (option.profile_id ? 'Для этой лодки' : 'Все лодки');
    dialog.querySelector('[data-option-description]').textContent = option.description || '';
    dialog.querySelector('[data-option-price]').textContent = Number(option.price).toLocaleString('ru-RU') + ' ₽ / лодку';
    if (!dialog.open) dialog.showModal();
  });
  dialog.querySelector('[data-close-option]').addEventListener('click', function () { dialog.close(); });
  dialog.addEventListener('click', function (event) {
    if (event.target !== dialog) return;
    var bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  });
})();
