/* Drag-and-drop reordering of the rows of a list (works and goods of a
   tuning order). A row is grabbed by its handle ([data-drag-handle]) with a
   mouse, a finger or a pen (pointer events, no HTML5 drag-and-drop, so touch
   screens work too); a focused handle also moves its row with ↑ / ↓.

     initSortableList(container, {
       rowSelector: ".work-row",        // the direct children that can move
       onDrop: function (rows) {...},   // called once after an order change
     });
*/
(function () {
  "use strict";

  function initSortableList(container, options) {
    if (!container || container.getAttribute("data-sortable-ready")) { return; }
    container.setAttribute("data-sortable-ready", "1");
    var rowSelector = options.rowSelector;
    var onDrop = options.onDrop || function () {};

    function rows() {
      return Array.prototype.filter.call(container.children, function (el) {
        return el.matches(rowSelector);
      });
    }
    function rowOf(handle) {
      var row = handle.closest(rowSelector);
      return row && row.parentElement === container ? row : null;
    }
    function snapshot() {
      return rows().map(function (row) { return row; });
    }
    function sameOrder(a, b) {
      return a.length === b.length && a.every(function (row, index) { return row === b[index]; });
    }

    // The row under the pointer is never re-inserted into the DOM: that would
    // detach the element holding the pointer capture and the browser would
    // stop sending it the pointer events. Instead the neighbour the pointer
    // has crossed (its middle line) is moved to the other side of the row —
    // the resulting order is the same. Checking only the immediate
    // neighbours keeps rows of different heights from jumping back and forth.
    function followPointer(row, y) {
      var moved = true;
      while (moved) {
        moved = false;
        var next = row.nextElementSibling;
        while (next && !next.matches(rowSelector)) { next = next.nextElementSibling; }
        if (next) {
          var nextBox = next.getBoundingClientRect();
          if (y > nextBox.top + nextBox.height / 2) {
            container.insertBefore(next, row);
            moved = true;
            continue;
          }
        }
        var prev = row.previousElementSibling;
        while (prev && !prev.matches(rowSelector)) { prev = prev.previousElementSibling; }
        if (prev) {
          var prevBox = prev.getBoundingClientRect();
          if (y < prevBox.top + prevBox.height / 2) {
            container.insertBefore(prev, row.nextElementSibling);
            moved = true;
          }
        }
      }
    }

    container.addEventListener("pointerdown", function (event) {
      var handle = event.target.closest("[data-drag-handle]");
      if (!handle || !container.contains(handle)) { return; }
      if (event.pointerType === "mouse" && event.button !== 0) { return; }
      var row = rowOf(handle);
      if (!row) { return; }
      event.preventDefault();
      var before = snapshot();
      var lastY = event.clientY;
      var scrollTimer = null;
      try { handle.setPointerCapture(event.pointerId); } catch (error) { /* not capturable */ }
      row.classList.add("is-dragging");
      container.classList.add("is-sorting");

      function autoScroll() {
        var edge = 70;
        var step = 0;
        if (lastY < edge) { step = -Math.ceil((edge - lastY) / 6); }
        else if (lastY > window.innerHeight - edge) { step = Math.ceil((lastY - (window.innerHeight - edge)) / 6); }
        if (step) {
          window.scrollBy(0, step);
          followPointer(row, lastY);
        }
        scrollTimer = window.requestAnimationFrame(autoScroll);
      }
      scrollTimer = window.requestAnimationFrame(autoScroll);

      function move(moveEvent) {
        if (moveEvent.pointerId !== event.pointerId) { return; }
        moveEvent.preventDefault();
        lastY = moveEvent.clientY;
        followPointer(row, lastY);
      }
      function finish(endEvent) {
        if (endEvent.pointerId !== event.pointerId) { return; }
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", finish);
        handle.removeEventListener("pointercancel", finish);
        window.cancelAnimationFrame(scrollTimer);
        row.classList.remove("is-dragging");
        container.classList.remove("is-sorting");
        if (!sameOrder(before, rows())) { onDrop(rows()); }
      }
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", finish);
      handle.addEventListener("pointercancel", finish);
    });

    container.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") { return; }
      var handle = event.target.closest("[data-drag-handle]");
      if (!handle || !container.contains(handle)) { return; }
      var row = rowOf(handle);
      if (!row) { return; }
      event.preventDefault();
      var list = rows();
      var index = list.indexOf(row);
      if (event.key === "ArrowUp" && index > 0) {
        container.insertBefore(row, list[index - 1]);
      } else if (event.key === "ArrowDown" && index < list.length - 1) {
        container.insertBefore(row, list[index + 1].nextElementSibling);
      } else {
        return;
      }
      handle.focus();
      onDrop(rows());
    });
  }

  window.initSortableList = initSortableList;
})();
