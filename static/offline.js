(function () {
  "use strict";

  var DB_NAME = "bodry-offline-v1";
  var DB_VERSION = 1;
  var PAGE_CACHE = "bodry-offline-pages-v1";
  var DOCUMENT_CACHE = "bodry-offline-documents-v1";
  var SYNC_TAG = "bodry-offline-outbox";
  var dbPromise = null;
  var state = {
    bootstrap: null,
    boatIndex: 0,
    activeOperation: null,
    syncing: false,
    savingAnswer: false,
    serverReachable: navigator.onLine,
  };

  function openDatabase() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise(function (resolve, reject) {
      var request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = function () {
        var db = request.result;
        if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta", {keyPath: "key"});
        if (!db.objectStoreNames.contains("operations")) db.createObjectStore("operations", {keyPath: "id"});
        if (!db.objectStoreNames.contains("attachments")) {
          var attachments = db.createObjectStore("attachments", {keyPath: "id"});
          attachments.createIndex("operationId", "operationId", {unique: false});
        }
      };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
    return dbPromise;
  }

  function requestResult(request) {
    return new Promise(function (resolve, reject) {
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }

  function storeAction(storeName, mode, callback) {
    return openDatabase().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(storeName, mode);
        var result;
        try {
          result = callback(tx.objectStore(storeName));
        } catch (error) {
          reject(error);
          return;
        }
        tx.oncomplete = function () { resolve(result); };
        tx.onerror = function () { reject(tx.error); };
        tx.onabort = function () { reject(tx.error || new Error("Локальная запись отменена.")); };
      });
    });
  }

  function getRecord(storeName, key) {
    return openDatabase().then(function (db) {
      return requestResult(db.transaction(storeName, "readonly").objectStore(storeName).get(key));
    });
  }

  function getAllRecords(storeName) {
    return openDatabase().then(function (db) {
      return requestResult(db.transaction(storeName, "readonly").objectStore(storeName).getAll());
    });
  }

  function putRecord(storeName, value) {
    return storeAction(storeName, "readwrite", function (store) { store.put(value); });
  }

  function deleteRecord(storeName, key) {
    return storeAction(storeName, "readwrite", function (store) { store.delete(key); });
  }

  function attachmentsFor(operationId) {
    return openDatabase().then(function (db) {
      var store = db.transaction("attachments", "readonly").objectStore("attachments");
      return requestResult(store.index("operationId").getAll(operationId));
    });
  }

  function deleteOperation(operationId) {
    return attachmentsFor(operationId).then(function (attachments) {
      return openDatabase().then(function (db) {
        return new Promise(function (resolve, reject) {
          var tx = db.transaction(["operations", "attachments"], "readwrite");
          tx.objectStore("operations").delete(operationId);
          attachments.forEach(function (attachment) {
            tx.objectStore("attachments").delete(attachment.id);
          });
          tx.oncomplete = resolve;
          tx.onerror = function () { reject(tx.error); };
        });
      });
    });
  }

  function uuid() {
    if (self.crypto && self.crypto.randomUUID) return self.crypto.randomUUID();
    var bytes = new Uint8Array(16);
    self.crypto.getRandomValues(bytes);
    return Array.prototype.map.call(bytes, function (byte) {
      return byte.toString(16).padStart(2, "0");
    }).join("");
  }

  function localTimestamp() {
    var now = new Date();
    function pad(value) { return String(value).padStart(2, "0"); }
    return now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate()) +
      " " + pad(now.getHours()) + ":" + pad(now.getMinutes());
  }

  function formatTimestamp(value) {
    if (!value) return "";
    var parts = value.split(" ");
    var date = (parts[0] || "").split("-");
    return date.length === 3 ? date[2] + "." + date[1] + " " + (parts[1] || "") : value;
  }

  function showToast(message, kind) {
    var toast = document.getElementById("offlineToast");
    if (!toast) return;
    toast.textContent = message;
    toast.className = "offline-toast offline-toast-" + (kind || "info");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(function () { toast.classList.add("hidden"); }, 5000);
  }

  function setConnectionState() {
    var online = navigator.onLine && state.serverReachable;
    var dot = document.getElementById("offlineSignalDot");
    var dashboardDot = document.getElementById("offlineDashboardSignal");
    var label = document.getElementById("offlineConnectionLabel");
    if (dot) dot.classList.toggle("is-online", online);
    if (dashboardDot) dashboardDot.classList.toggle("is-online", online);
    if (label) label.textContent = online ? "Связь есть" : "Нет связи — данные сохраняются на устройстве";
    document.querySelectorAll("[data-offline-connection]").forEach(function (element) {
      element.textContent = online ? "Онлайн" : "Офлайн";
      element.classList.toggle("is-online", online);
    });
  }

  function pendingOperations() {
    return getAllRecords("operations").then(function (operations) {
      return operations.filter(function (operation) {
        return operation.status === "queued" || operation.status === "error" || operation.status === "blocked";
      });
    });
  }

  function refreshPendingStatus() {
    return pendingOperations().then(function (operations) {
      var count = operations.length;
      var countElement = document.getElementById("offlinePendingCount");
      if (countElement) countElement.textContent = String(count);
      document.querySelectorAll("[data-offline-pending]").forEach(function (element) {
        element.textContent = count ? count + " ждут отправки" : "Всё отправлено";
        element.classList.toggle("has-pending", count > 0);
      });
      return count;
    });
  }

  function cacheWorkspacePage() {
    if (!("caches" in window) || !navigator.onLine) return Promise.resolve();
    return fetch("/team/offline", {credentials: "same-origin"}).then(function (response) {
      if (!response.ok || response.redirected) throw new Error("Не удалось подготовить офлайн-экран.");
      return caches.open(PAGE_CACHE).then(function (cache) {
        return cache.put("/team/offline", response.clone());
      });
    });
  }

  function loadStoredBootstrap() {
    return getRecord("meta", "bootstrap").then(function (record) {
      if (record && record.value) state.bootstrap = record.value;
      return state.bootstrap;
    });
  }

  function clearWorkspaceForAnotherEmployee() {
    return openDatabase().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(["meta", "operations", "attachments"], "readwrite");
        tx.objectStore("meta").clear();
        tx.objectStore("operations").clear();
        tx.objectStore("attachments").clear();
        tx.oncomplete = resolve;
        tx.onerror = function () { reject(tx.error); };
      });
    }).then(function () {
      if (!("caches" in window)) return;
      return Promise.all([caches.delete(PAGE_CACHE), caches.delete(DOCUMENT_CACHE)]);
    });
  }

  function refreshBootstrap() {
    if (!navigator.onLine) return Promise.resolve(state.bootstrap);
    return fetch("/api/offline/bootstrap", {
      credentials: "same-origin",
      headers: {Accept: "application/json"},
    }).then(function (response) {
      if (!response.ok || response.redirected) throw new Error("Войдите в личный кабинет заново.");
      return response.json();
    }).then(function (payload) {
      if (!payload.ok) throw new Error(payload.error || "Не удалось обновить данные.");
      state.serverReachable = true;
      setConnectionState();
      var previousEmployee = state.bootstrap && state.bootstrap.employee && state.bootstrap.employee.name;
      var nextEmployee = payload.employee && payload.employee.name;
      var prepare = previousEmployee && previousEmployee !== nextEmployee
        ? clearWorkspaceForAnotherEmployee()
        : Promise.resolve();
      return prepare.then(function () {
        state.bootstrap = payload;
        return putRecord("meta", {key: "bootstrap", value: payload});
      }).then(function () {
        return cacheWorkspacePage().catch(function () {});
      }).then(function () {
        if ("serviceWorker" in navigator) return navigator.serviceWorker.ready;
      }).then(function () { return payload; });
    }).catch(function (error) {
      state.serverReachable = false;
      setConnectionState();
      if (error && error.message === "Failed to fetch") {
        throw new Error("Сервер недоступен. Используем сохранённые данные.");
      }
      throw error;
    });
  }

  function registerBackgroundSync() {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.ready.then(function (registration) {
      if (registration.sync) return registration.sync.register(SYNC_TAG);
    }).catch(function () {});
  }

  function syncOne(operation) {
    return attachmentsFor(operation.id).then(function (attachments) {
      var form = new FormData();
      form.append("operation", JSON.stringify(operation));
      attachments.forEach(function (attachment) {
        form.append("attachments", attachment.blob, attachment.filename);
      });
      return fetch("/api/offline/sync", {
        method: "POST",
        credentials: "same-origin",
        body: form,
      });
    }).then(function (response) {
      if (response.redirected || response.status === 401 || response.status === 403) {
        var authError = new Error("Сессия завершилась. Войдите снова — очередь сохранена.");
        authError.auth = true;
        throw authError;
      }
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (!response.ok || !payload.ok) {
          var error = new Error(payload.error || "Сервер не принял данные.");
          error.retryable = payload.retryable !== false;
          throw error;
        }
        state.serverReachable = true;
        setConnectionState();
        return deleteOperation(operation.id);
      });
    }).catch(function (error) {
      if (error && error.message === "Failed to fetch") {
        state.serverReachable = false;
        setConnectionState();
        error = new Error("Сервер недоступен. Данные сохранены на устройстве.");
      }
      operation.status = error.retryable === false ? "blocked" : "error";
      operation.lastError = error.message || "Нет связи с сервером.";
      operation.updatedAt = localTimestamp();
      return putRecord("operations", operation).then(function () { throw error; });
    });
  }

  function syncQueue() {
    if (state.syncing || !navigator.onLine) {
      refreshPendingStatus();
      return Promise.resolve();
    }
    state.syncing = true;
    var button = document.getElementById("offlineSyncButton");
    if (button) {
      button.disabled = true;
      button.textContent = "Отправляем…";
    }
    return getAllRecords("operations").then(function (operations) {
      var queue = operations.filter(function (operation) {
        return operation.status === "queued" || operation.status === "error";
      }).sort(function (left, right) { return left.created_at.localeCompare(right.created_at); });
      var chain = Promise.resolve();
      var sent = 0;
      queue.forEach(function (operation) {
        chain = chain.then(function () {
          return syncOne(operation).then(function () {
            sent += 1;
          }).catch(function (error) {
            if (error.retryable === false) return;
            throw error;
          });
        });
      });
      return chain.then(function () {
        if (sent) showToast("Данные отправлены: " + sent + ".", "success");
      });
    }).catch(function (error) {
      showToast(error.message || "Не удалось отправить очередь. Данные сохранены.", "error");
    }).finally(function () {
      state.syncing = false;
      if (button) {
        button.disabled = false;
        button.textContent = "Синхронизировать";
      }
      refreshPendingStatus();
      renderOutbox();
    });
  }

  function currentBoat() {
    if (!state.bootstrap || !state.bootstrap.boats) return null;
    return state.bootstrap.boats[state.boatIndex] || state.bootstrap.boats[0] || null;
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function documentCached(url) {
    if (!("caches" in window)) return Promise.resolve(false);
    return caches.open(DOCUMENT_CACHE).then(function (cache) {
      return cache.match(url).then(Boolean);
    });
  }

  function renderDocuments() {
    var boat = currentBoat();
    var list = document.getElementById("offlineDocumentList");
    var summary = document.getElementById("offlineDocumentSummary");
    var download = document.getElementById("offlineDownloadDocuments");
    if (!list || !boat) return Promise.resolve();
    list.innerHTML = "";
    var documents = boat.documents || [];
    if (!documents.length) {
      list.appendChild(element("p", "captain-fleet-empty", "Документы по этому судну ещё не загружены."));
      summary.textContent = "Документов нет.";
      download.disabled = true;
      return Promise.resolve();
    }
    download.disabled = false;
    return Promise.all(documents.map(function (document) {
      return documentCached(document.url).then(function (cached) {
        var row = element("div", "offline-document-row");
        var link = element("a", "offline-document-link", document.title);
        link.href = document.url;
        link.target = "_blank";
        link.rel = "noopener";
        link.addEventListener("click", function (event) {
          if (!navigator.onLine && !cached) {
            event.preventDefault();
            showToast("Этот документ ещё не скачан на устройство.", "error");
          }
        });
        row.appendChild(link);
        row.appendChild(element("span", "offline-document-state " + (cached ? "is-ready" : ""), cached ? "На устройстве" : "Только онлайн"));
        list.appendChild(row);
        return cached;
      });
    })).then(function (states) {
      var ready = states.filter(Boolean).length;
      summary.textContent = "На устройстве: " + ready + " из " + documents.length + ".";
      download.textContent = ready === documents.length ? "Обновить локальные копии" : "Скачать документы на устройство";
    });
  }

  function downloadDocuments() {
    var boat = currentBoat();
    var button = document.getElementById("offlineDownloadDocuments");
    if (!boat || !navigator.onLine) {
      showToast("Для загрузки документов нужен интернет.", "error");
      return;
    }
    button.disabled = true;
    button.textContent = "Загружаем…";
    caches.open(DOCUMENT_CACHE).then(function (cache) {
      var chain = Promise.resolve();
      (boat.documents || []).forEach(function (document) {
        chain = chain.then(function () {
          return fetch(document.url, {credentials: "same-origin"}).then(function (response) {
            if (!response.ok || response.redirected) throw new Error("Не удалось скачать «" + document.title + "».");
            return cache.put(document.url, response.clone());
          });
        });
      });
      return chain;
    }).then(function () {
      showToast("Документы судна доступны без связи.", "success");
    }).catch(function (error) {
      showToast(error.message, "error");
    }).finally(function () {
      button.disabled = false;
      renderDocuments();
    });
  }

  function renderDrafts() {
    var container = document.getElementById("offlineDrafts");
    var boat = currentBoat();
    if (!container || !boat) return Promise.resolve();
    return getAllRecords("operations").then(function (operations) {
      container.innerHTML = "";
      operations.filter(function (operation) {
        return operation.status === "draft" && operation.payload.boat === boat.name;
      }).forEach(function (operation) {
        var row = element("div", "offline-draft-row");
        var details = element("span", "", operation.payload.checklist_label + " · " + operation.payload.answers.length + " ответов");
        var resume = element("button", "btn-secondary", "Продолжить");
        resume.type = "button";
        resume.addEventListener("click", function () {
          state.activeOperation = operation;
          renderChecklist();
        });
        row.appendChild(details);
        row.appendChild(resume);
        container.appendChild(row);
      });
    });
  }

  function renderBoat() {
    var boat = currentBoat();
    if (!boat) return;
    var select = document.getElementById("offlineBoatSelect");
    if (select) select.value = String(state.boatIndex);
    var callout = document.getElementById("offlineBoatCallout");
    if (callout) callout.textContent = "Выбрано: " + boat.name;
    putRecord("meta", {key: "selectedBoat", value: state.boatIndex});
    renderDocuments();
    renderDrafts();
  }

  function startChecklist(checklistType) {
    var boat = currentBoat();
    if (!boat || !boat.checklists[checklistType]) {
      showToast("Шаблон чек-листа ещё не загружен. Подключитесь к интернету.", "error");
      return;
    }
    getAllRecords("operations").then(function (operations) {
      var existing = operations.find(function (operation) {
        return operation.status === "draft" && operation.payload.boat === boat.name && operation.payload.checklist_type === checklistType;
      });
      if (existing) return existing;
      var checklist = boat.checklists[checklistType];
      var operation = {
        id: uuid(),
        type: "checklist",
        status: "draft",
        created_at: localTimestamp(),
        payload: {
          boat: boat.name,
          checklist_type: checklistType,
          checklist_label: checklist.label,
          template_version: state.bootstrap.template_version,
          started_at: localTimestamp(),
          completed_at: null,
          questions: checklist.questions,
          answers: [],
          extra_defects: [],
        },
      };
      return putRecord("operations", operation).then(function () { return operation; });
    }).then(function (operation) {
      state.activeOperation = operation;
      renderChecklist();
    }).catch(function () {
      showToast("Не удалось сохранить черновик на устройстве.", "error");
    });
  }

  function renderChecklist() {
    var operation = state.activeOperation;
    var run = document.getElementById("offlineChecklistRun");
    var modules = document.getElementById("offlineModules");
    if (!run || !operation) return;
    modules.classList.add("hidden");
    run.classList.remove("hidden");
    document.getElementById("offlineChecklistLabel").textContent = operation.payload.checklist_label;
    document.getElementById("offlineChecklistBoat").textContent = operation.payload.boat;
    var questions = operation.payload.questions;
    var index = operation.payload.answers.length;
    var questionPanel = document.getElementById("offlineQuestionPanel");
    var finishPanel = document.getElementById("offlineChecklistFinish");
    document.getElementById("offlineProblemForm").classList.add("hidden");
    document.getElementById("offlineAnswerActions").classList.remove("hidden");
    if (index >= questions.length) {
      questionPanel.classList.add("hidden");
      finishPanel.classList.remove("hidden");
      var problems = operation.payload.answers.filter(function (answer) { return answer.status === "problem"; }).length;
      document.getElementById("offlineChecklistResult").textContent = problems ? "Обнаружено проблем: " + problems + "." : "Все пункты отмечены исправными.";
    } else {
      finishPanel.classList.add("hidden");
      questionPanel.classList.remove("hidden");
      var question = questions[index];
      document.getElementById("offlineQuestionCounter").textContent = "Пункт " + (index + 1) + " из " + questions.length;
      document.getElementById("offlineProgressValue").style.width = Math.round(index / questions.length * 100) + "%";
      document.getElementById("offlineQuestionTitle").textContent = question.title || "Проверка";
      document.getElementById("offlineQuestionText").textContent = question.text;
    }
    run.scrollIntoView({behavior: "smooth", block: "start"});
    renderDrafts();
  }

  function saveAnswer(status, comment, photoIds) {
    if (state.savingAnswer) return Promise.resolve();
    state.savingAnswer = true;
    var operation = state.activeOperation;
    var index = operation.payload.answers.length;
    var question = operation.payload.questions[index];
    operation.payload.answers.push({
      question_index: index,
      question_title: question.title || "",
      question_text: question.text,
      status: status,
      comment: comment || "",
      photo_ids: photoIds || [],
    });
    operation.updatedAt = localTimestamp();
    return putRecord("operations", operation).then(renderChecklist).finally(function () {
      state.savingAnswer = false;
    });
  }

  function imageExtension(file) {
    var match = (file.name || "").toLowerCase().match(/\.(jpg|jpeg|png|webp)$/);
    return match ? "." + match[1] : ".jpg";
  }

  function compressImage(file) {
    if (!file.type || file.type.indexOf("image/") !== 0) return Promise.reject(new Error("Можно прикреплять только изображения."));
    if (!("createImageBitmap" in window)) {
      if (!/\.(jpe?g|png|webp)$/i.test(file.name || "")) {
        return Promise.reject(new Error("Формат этой фотографии не поддерживается."));
      }
      return Promise.resolve({blob: file, extension: imageExtension(file)});
    }
    return createImageBitmap(file).then(function (bitmap) {
      var maxSide = 1600;
      var scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
      var canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      bitmap.close();
      return new Promise(function (resolve, reject) {
        canvas.toBlob(function (blob) {
          if (blob) resolve({blob: blob, extension: ".jpg"});
          else reject(new Error("Не удалось обработать фотографию."));
        }, "image/jpeg", 0.78);
      });
    }).catch(function () {
      var extension = imageExtension(file);
      if (extension === ".jpg" && !/\.jpe?g$/i.test(file.name || "")) {
        throw new Error("Формат этой фотографии не поддерживается.");
      }
      return {blob: file, extension: extension};
    });
  }

  function storeProblemPhotos(files) {
    var operationId = state.activeOperation.id;
    var tasks = Array.prototype.map.call(files || [], function (file) {
      return compressImage(file).then(function (prepared) {
        var attachmentId = uuid();
        return putRecord("attachments", {
          id: attachmentId,
          operationId: operationId,
          filename: attachmentId + prepared.extension,
          blob: prepared.blob,
          size: prepared.blob.size,
        }).then(function () { return attachmentId; });
      });
    });
    return Promise.all(tasks).then(function (ids) {
      return attachmentsFor(operationId).then(function (attachments) {
        var total = attachments.reduce(function (sum, attachment) { return sum + (attachment.size || 0); }, 0);
        if (total > 14 * 1024 * 1024) {
          return Promise.all(ids.map(function (id) {
            return deleteRecord("attachments", id);
          })).then(function () {
            throw new Error("Фотографии занимают больше 14 МБ. Уменьшите их количество.");
          });
        }
        return ids;
      });
    });
  }

  function finishChecklist() {
    var operation = state.activeOperation;
    var extra = document.getElementById("offlineExtraDefects").value.split("\n").map(function (item) {
      return item.trim();
    }).filter(Boolean);
    operation.payload.extra_defects = extra;
    operation.payload.completed_at = localTimestamp();
    operation.status = "queued";
    operation.updatedAt = localTimestamp();
    return putRecord("operations", operation).then(function () {
      state.activeOperation = null;
      document.getElementById("offlineChecklistRun").classList.add("hidden");
      document.getElementById("offlineModules").classList.remove("hidden");
      document.getElementById("offlineExtraDefects").value = "";
      showToast("Осмотр сохранён на устройстве и поставлен в очередь.", "success");
      registerBackgroundSync();
      refreshPendingStatus();
      renderOutbox();
      renderDrafts();
      syncQueue();
    });
  }

  function createDefect(description) {
    var boat = currentBoat();
    var operation = {
      id: uuid(),
      type: "defect",
      status: "queued",
      created_at: localTimestamp(),
      payload: {
        boat: boat.name,
        description: description.trim(),
        reported_at: localTimestamp(),
      },
    };
    return putRecord("operations", operation).then(function () {
      showToast("Неисправность сохранена на устройстве.", "success");
      registerBackgroundSync();
      refreshPendingStatus();
      renderOutbox();
      return syncQueue();
    });
  }

  function renderOutbox() {
    var container = document.getElementById("offlineOutbox");
    if (!container) return Promise.resolve();
    return pendingOperations().then(function (operations) {
      container.innerHTML = "";
      if (!operations.length) {
        container.appendChild(element("p", "captain-fleet-empty", "Очередь пуста — все данные отправлены."));
        return;
      }
      operations.sort(function (left, right) { return right.created_at.localeCompare(left.created_at); }).forEach(function (operation) {
        var row = element("div", "offline-outbox-row");
        var copy = element("div", "offline-outbox-copy");
        var title = operation.type === "checklist" ? operation.payload.checklist_label : "Неисправность";
        copy.appendChild(element("strong", "", title + " · " + operation.payload.boat));
        copy.appendChild(element("small", "", formatTimestamp(operation.created_at) + (operation.lastError ? " · " + operation.lastError : "")));
        row.appendChild(copy);
        var status = operation.status === "blocked" ? "Нужно проверить" : operation.status === "error" ? "Повторим" : "В очереди";
        row.appendChild(element("span", "offline-outbox-status status-" + operation.status, status));
        if (operation.status === "blocked") {
          var discard = element("button", "icon-btn danger", "×");
          discard.type = "button";
          discard.title = "Удалить из очереди";
          discard.addEventListener("click", function () {
            if (!window.confirm("Удалить эту операцию с устройства? Отправить её после этого будет невозможно.")) return;
            deleteOperation(operation.id).then(function () {
              refreshPendingStatus();
              renderOutbox();
            });
          });
          row.appendChild(discard);
        }
        container.appendChild(row);
      });
    });
  }

  function hydrateWorkspace() {
    var snapshot = document.getElementById("offlineSnapshotLabel");
    if (!state.bootstrap) {
      if (snapshot) snapshot.textContent = "Нет сохранённых данных — один раз откройте экран с интернетом";
      document.getElementById("offlineModules").classList.add("is-disabled");
      return;
    }
    document.getElementById("offlineModules").classList.remove("is-disabled");
    if (snapshot) snapshot.textContent = "Данные обновлены: " + state.bootstrap.generated_at;
    var storedBoat = getRecord("meta", "selectedBoat");
    storedBoat.then(function (record) {
      var index = record && Number(record.value);
      state.boatIndex = Number.isInteger(index) && state.bootstrap.boats[index] ? index : 0;
      renderBoat();
    });
    renderOutbox();
  }

  function bindWorkspace() {
    var select = document.getElementById("offlineBoatSelect");
    select.addEventListener("change", function () {
      state.boatIndex = Number(select.value) || 0;
      renderBoat();
    });
    document.querySelectorAll("[data-start-checklist]").forEach(function (button) {
      button.addEventListener("click", function () { startChecklist(button.dataset.startChecklist); });
    });
    document.getElementById("offlineDownloadDocuments").addEventListener("click", downloadDocuments);
    document.getElementById("offlineSyncButton").addEventListener("click", syncQueue);
    document.getElementById("offlineDefectForm").addEventListener("submit", function (event) {
      event.preventDefault();
      var description = document.getElementById("offlineDefectDescription");
      if (!description.value.trim()) return;
      var button = event.currentTarget.querySelector('button[type="submit"]');
      button.disabled = true;
      createDefect(description.value).then(function () {
        description.value = "";
      }).finally(function () {
        button.disabled = false;
      });
    });
    document.getElementById("offlineAnswerOk").addEventListener("click", function () { saveAnswer("ok", "", []); });
    document.getElementById("offlineShowProblem").addEventListener("click", function () {
      document.getElementById("offlineAnswerActions").classList.add("hidden");
      document.getElementById("offlineProblemForm").classList.remove("hidden");
      document.getElementById("offlineProblemComment").focus();
    });
    document.getElementById("offlineProblemBack").addEventListener("click", function () {
      document.getElementById("offlineProblemForm").classList.add("hidden");
      document.getElementById("offlineAnswerActions").classList.remove("hidden");
    });
    document.getElementById("offlineProblemForm").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = event.currentTarget;
      var comment = document.getElementById("offlineProblemComment").value.trim();
      var files = document.getElementById("offlineProblemPhotos").files;
      var submit = form.querySelector('button[type="submit"]');
      submit.disabled = true;
      submit.textContent = "Сохраняем…";
      storeProblemPhotos(files).then(function (photoIds) {
        return saveAnswer("problem", comment, photoIds);
      }).then(function () {
        form.reset();
      }).catch(function (error) {
        showToast(error.message || "Не удалось сохранить фотографии.", "error");
      }).finally(function () {
        submit.disabled = false;
        submit.textContent = "Сохранить проблему";
      });
    });
    document.getElementById("offlineFinishChecklist").addEventListener("click", function (event) {
      event.currentTarget.disabled = true;
      Promise.resolve(finishChecklist()).finally(function () {
        event.currentTarget.disabled = false;
      });
    });
    document.getElementById("offlineCancelChecklist").addEventListener("click", function () {
      if (!state.activeOperation || !window.confirm("Удалить незавершённый чек-лист с устройства?")) return;
      deleteOperation(state.activeOperation.id).then(function () {
        state.activeOperation = null;
        document.getElementById("offlineChecklistRun").classList.add("hidden");
        document.getElementById("offlineModules").classList.remove("hidden");
        renderDrafts();
      });
    });
  }

  function clearOfflineData() {
    var tasks = [];
    if ("caches" in window) {
      tasks.push(caches.keys().then(function (names) {
        return Promise.all(names.filter(function (name) { return name.indexOf("bodry-offline-") === 0; }).map(function (name) { return caches.delete(name); }));
      }));
    }
    tasks.push(openDatabase().then(function (db) {
      db.close();
      dbPromise = null;
      return new Promise(function (resolve) {
        var request = indexedDB.deleteDatabase(DB_NAME);
        request.onsuccess = request.onerror = request.onblocked = resolve;
      });
    }));
    return Promise.all(tasks);
  }

  function bindLogout() {
    document.querySelectorAll("[data-offline-logout]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (form.dataset.clearing === "1") return;
        event.preventDefault();
        clearOfflineData().finally(function () {
          form.dataset.clearing = "1";
          form.submit();
        });
      });
    });
  }

  function initialize() {
    setConnectionState();
    bindLogout();
    if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(function () {});
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.addEventListener("message", function (event) {
        if (event.data && event.data.type === "offline-sync-complete") {
          refreshPendingStatus();
          renderOutbox();
        }
      });
    }
    var workspace = document.getElementById("offlineWorkspace");
    if (workspace) bindWorkspace();
    loadStoredBootstrap().then(function () {
      if (workspace) hydrateWorkspace();
      return refreshBootstrap();
    }).then(function () {
      if (workspace) hydrateWorkspace();
      document.querySelectorAll("[data-offline-ready]").forEach(function (element) {
        element.textContent = "Офлайн-режим готов";
        element.classList.add("is-ready");
      });
    }).catch(function (error) {
      if (!state.bootstrap && workspace) hydrateWorkspace();
      document.querySelectorAll("[data-offline-ready]").forEach(function (element) {
        element.textContent = state.bootstrap ? "Работаем по сохранённым данным" : "Нужно один раз подготовить с интернетом";
      });
      if (navigator.onLine && workspace) showToast(error.message, "error");
    }).finally(function () {
      refreshPendingStatus();
      syncQueue();
    });
  }

  window.addEventListener("online", function () {
    state.serverReachable = true;
    setConnectionState();
    refreshBootstrap().then(function () {
      if (document.getElementById("offlineWorkspace")) hydrateWorkspace();
    }).catch(function () {}).finally(syncQueue);
  });
  window.addEventListener("offline", function () {
    state.serverReachable = false;
    setConnectionState();
  });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") syncQueue();
  });
  window.addEventListener("load", initialize);
})();
