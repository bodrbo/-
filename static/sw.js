var SHELL_CACHE = "bodry-offline-shell-v2";
var PAGE_CACHE = "bodry-offline-pages-v1";
var DOCUMENT_CACHE = "bodry-offline-documents-v1";
var DB_NAME = "bodry-offline-v1";
var DB_VERSION = 1;
var SYNC_TAG = "bodry-offline-outbox";

var SHELL_ASSETS = [
  "/static/style.css",
  "/static/loader.js",
  "/static/offline.js",
  "/static/manifest.json",
  "/static/logo.png",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/fonts/open-sans-cyrillic.woff2",
  "/static/fonts/open-sans-latin.woff2",
  "/static/fonts/pt-mono-400.woff2"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(function (cache) {
      return cache.addAll(SHELL_ASSETS);
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(names.filter(function (name) {
        return name.indexOf("bodry-offline-") === 0 &&
          name !== SHELL_CACHE && name !== PAGE_CACHE && name !== DOCUMENT_CACHE;
      }).map(function (name) { return caches.delete(name); }));
    }).then(function () { return self.clients.claim(); })
  );
});

function networkFirstWorkspace(request) {
  return fetch(request).then(function (response) {
    if (response.ok && !response.redirected) {
      caches.open(PAGE_CACHE).then(function (cache) {
        cache.put("/team/offline", response.clone());
      });
    }
    return response;
  }).catch(function () {
    return caches.open(PAGE_CACHE).then(function (cache) {
      return cache.match("/team/offline");
    }).then(function (cached) {
      return cached || Response.error();
    });
  });
}

function cachedDocument(request) {
  return caches.open(DOCUMENT_CACHE).then(function (cache) {
    return cache.match(request).then(function (cached) {
      if (cached) return cached;
      return fetch(request).then(function (response) {
        if (response.ok && !response.redirected) cache.put(request, response.clone());
        return response;
      });
    });
  });
}

function cachedShell(request) {
  return caches.open(SHELL_CACHE).then(function (cache) {
    return cache.match(request).then(function (cached) {
      var update = fetch(request).then(function (response) {
        if (response.ok) cache.put(request, response.clone());
        return response;
      }).catch(function () { return cached || Response.error(); });
      return cached || update;
    });
  });
}

function networkFirstShell(request) {
  return fetch(request).then(function (response) {
    if (response.ok) {
      caches.open(SHELL_CACHE).then(function (cache) {
        cache.put(request, response.clone());
      });
    }
    return response;
  }).catch(function () {
    return caches.open(SHELL_CACHE).then(function (cache) {
      return cache.match(request).then(function (cached) {
        if (cached) return cached;
        return cache.match(new URL(request.url).pathname);
      });
    }).then(function (cached) { return cached || Response.error(); });
  });
}

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") return;
  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.indexOf("/team/documents/boat/") === 0) {
    event.respondWith(cachedDocument(request));
    return;
  }
  if (url.pathname === "/team/offline") {
    event.respondWith(networkFirstWorkspace(request));
    return;
  }
  if (request.mode === "navigate" && url.pathname.indexOf("/team/") === 0) {
    event.respondWith(fetch(request).catch(function () {
      return caches.open(PAGE_CACHE).then(function (cache) {
        return cache.match("/team/offline");
      }).then(function (cached) { return cached || Response.error(); });
    }));
    return;
  }
  if (
    url.pathname === "/static/style.css" ||
    url.pathname === "/static/loader.js" ||
    url.pathname === "/static/offline.js"
  ) {
    event.respondWith(networkFirstShell(request));
    return;
  }
  if (
    url.pathname === "/static/manifest.json" ||
    url.pathname.indexOf("/static/fonts/") === 0 ||
    url.pathname === "/static/logo.png" ||
    url.pathname === "/static/icon-192.png" ||
    url.pathname === "/static/icon-512.png"
  ) {
    event.respondWith(cachedShell(request));
  }
});

function openDatabase() {
  return new Promise(function (resolve, reject) {
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
}

function requestResult(request) {
  return new Promise(function (resolve, reject) {
    request.onsuccess = function () { resolve(request.result); };
    request.onerror = function () { reject(request.error); };
  });
}

function allOperations(db) {
  return requestResult(db.transaction("operations", "readonly").objectStore("operations").getAll());
}

function operationAttachments(db, operationId) {
  var store = db.transaction("attachments", "readonly").objectStore("attachments");
  return requestResult(store.index("operationId").getAll(operationId));
}

function saveOperation(db, operation) {
  return new Promise(function (resolve, reject) {
    var tx = db.transaction("operations", "readwrite");
    tx.objectStore("operations").put(operation);
    tx.oncomplete = resolve;
    tx.onerror = function () { reject(tx.error); };
  });
}

function removeOperation(db, operationId, attachments) {
  return new Promise(function (resolve, reject) {
    var tx = db.transaction(["operations", "attachments"], "readwrite");
    tx.objectStore("operations").delete(operationId);
    attachments.forEach(function (attachment) {
      tx.objectStore("attachments").delete(attachment.id);
    });
    tx.oncomplete = resolve;
    tx.onerror = function () { reject(tx.error); };
  });
}

function replayOperation(db, operation) {
  return operationAttachments(db, operation.id).then(function (attachments) {
    var form = new FormData();
    form.append("operation", JSON.stringify(operation));
    attachments.forEach(function (attachment) {
      form.append("attachments", attachment.blob, attachment.filename);
    });
    return fetch("/api/offline/sync", {
      method: "POST",
      credentials: "same-origin",
      body: form,
    }).then(function (response) {
      if (response.redirected || response.status === 401 || response.status === 403) {
        throw new Error("authorization-required");
      }
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (response.ok && payload.ok) return removeOperation(db, operation.id, attachments);
        operation.status = payload.retryable === false ? "blocked" : "error";
        operation.lastError = payload.error || "Сервер не принял данные.";
        return saveOperation(db, operation).then(function () {
          if (payload.retryable !== false) throw new Error(operation.lastError);
        });
      });
    });
  });
}

function broadcastSyncComplete() {
  return self.clients.matchAll({type: "window", includeUncontrolled: true}).then(function (clients) {
    clients.forEach(function (client) {
      client.postMessage({type: "offline-sync-complete"});
    });
  });
}

function flushOutbox() {
  return openDatabase().then(function (db) {
    return allOperations(db).then(function (operations) {
      var queue = operations.filter(function (operation) {
        return operation.status === "queued" || operation.status === "error";
      }).sort(function (left, right) { return left.created_at.localeCompare(right.created_at); });
      var chain = Promise.resolve();
      queue.forEach(function (operation) {
        chain = chain.then(function () { return replayOperation(db, operation); });
      });
      return chain.finally(function () { db.close(); });
    });
  }).then(broadcastSyncComplete);
}

self.addEventListener("sync", function (event) {
  if (event.tag === SYNC_TAG) event.waitUntil(flushOutbox());
});

// Push notifications — server sends {title, body, url} as JSON.
self.addEventListener("push", function (event) {
  var payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (error) {
    payload = {body: event.data ? event.data.text() : ""};
  }
  event.waitUntil(self.registration.showNotification(payload.title || "Бодрый Боцман", {
    body: payload.body || "",
    icon: "/static/icon-192.png",
    badge: "/static/icon-192.png",
    data: {url: payload.url || "/"},
  }));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({type: "window", includeUncontrolled: true}).then(function (clients) {
      for (var index = 0; index < clients.length; index += 1) {
        if (clients[index].url.indexOf(url) !== -1 && "focus" in clients[index]) return clients[index].focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});
