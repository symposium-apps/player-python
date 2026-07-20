const DB_NAME = "jukebox-audio-cache-worker-v1";
const MAX_BYTES = 500 * 1024 * 1024;

let configuredGeneration = "";
let configuredTicket = "";
let configuredBudget = MAX_BYTES;
let desiredKeys = new Set();
let activeDownload = null;
let downloadChain = Promise.resolve();

self.addEventListener("install", event => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("IndexedDB request failed"));
  });
}

function transactionDone(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error || new Error("IndexedDB transaction failed"));
    transaction.onabort = () => reject(transaction.error || new Error("IndexedDB transaction aborted"));
  });
}

async function openDb() {
  const request = indexedDB.open(DB_NAME, 2);
  request.onupgradeneeded = () => {
    const db = request.result;
    if (!db.objectStoreNames.contains("entries")) {
      const entries = db.createObjectStore("entries", { keyPath: "key" });
      entries.createIndex("trackId", "trackId", { unique: false });
    }
    if (!db.objectStoreNames.contains("settings")) db.createObjectStore("settings", { keyPath: "key" });
    if (!db.objectStoreNames.contains("audio")) db.createObjectStore("audio", { keyPath: "key" });
  };
  return requestResult(request);
}

async function dbGet(storeName, key) {
  const db = await openDb();
  const transaction = db.transaction(storeName, "readonly");
  const value = await requestResult(transaction.objectStore(storeName).get(key));
  db.close();
  return value;
}

async function dbGetAll(storeName) {
  const db = await openDb();
  const transaction = db.transaction(storeName, "readonly");
  const values = await requestResult(transaction.objectStore(storeName).getAll());
  db.close();
  return values;
}

async function dbPut(storeName, value) {
  const db = await openDb();
  const transaction = db.transaction(storeName, "readwrite");
  const done = transactionDone(transaction);
  await requestResult(transaction.objectStore(storeName).put(value));
  await done;
  db.close();
}

async function dbDelete(storeName, key) {
  const db = await openDb();
  const transaction = db.transaction(storeName, "readwrite");
  const done = transactionDone(transaction);
  await requestResult(transaction.objectStore(storeName).delete(key));
  await done;
  db.close();
}

async function dbClear(storeName) {
  const db = await openDb();
  const transaction = db.transaction(storeName, "readwrite");
  const done = transactionDone(transaction);
  await requestResult(transaction.objectStore(storeName).clear());
  await done;
  db.close();
}

async function removeEntry(entry) {
  await dbDelete("audio", entry.key);
  await dbDelete("entries", entry.key);
}

async function cacheStatus() {
  const entries = await dbGetAll("entries");
  const bytes = entries.reduce((total, entry) => total + Number(entry.size || 0), 0);
  return {
    type: "audio-cache-status",
    bytes,
    count: entries.length,
    budget: configuredBudget,
    trackIds: entries.map(entry => String(entry.trackId))
  };
}

async function broadcastStatus() {
  const status = await cacheStatus();
  const clients = await self.clients.matchAll({ includeUncontrolled: true, type: "window" });
  clients.forEach(client => client.postMessage(status));
}

async function clearStoredAudio(generation = configuredGeneration) {
  if (activeDownload) activeDownload.controller.abort();
  await dbClear("audio");
  await dbClear("entries");
  await dbPut("settings", { key: "generation", value: String(generation || "") });
  await broadcastStatus();
}

async function configure(message) {
  const nextGeneration = String(message.generation || "");
  const savedGeneration = String((await dbGet("settings", "generation"))?.value || "");
  configuredGeneration = nextGeneration;
  configuredTicket = String(message.ticket || "");
  configuredBudget = Math.max(0, Math.min(MAX_BYTES, Number(message.budget || MAX_BYTES)));
  if (savedGeneration !== nextGeneration) await clearStoredAudio(nextGeneration);
  else await enforceBudget(0);
  await broadcastStatus();
}

async function enforceBudget(requiredBytes) {
  let entries = await dbGetAll("entries");
  let bytes = entries.reduce((total, entry) => total + Number(entry.size || 0), 0);
  entries = entries
    .filter(entry => !desiredKeys.has(entry.key) && entry.key !== activeDownload?.key)
    .sort((left, right) => Number(left.lastAccess || 0) - Number(right.lastAccess || 0));
  while (bytes + requiredBytes > configuredBudget && entries.length) {
    const entry = entries.shift();
    await removeEntry(entry);
    bytes -= Number(entry.size || 0);
  }
  return bytes + requiredBytes <= configuredBudget;
}

async function cacheOneTrack(track) {
  const id = String(track.id || "");
  const size = Number(track.size || 0);
  const modified = Number(track.modified || 0);
  const generation = String(track.generation || "");
  const key = `${id}:${size}:${modified}`;
  if (!id || !size || size > configuredBudget || generation !== configuredGeneration || !configuredTicket) return;
  const existing = await dbGet("entries", key);
  if (existing) {
    existing.lastAccess = Date.now();
    await dbPut("entries", existing);
    return;
  }
  if (!await enforceBudget(size)) return;
  const controller = new AbortController();
  activeDownload = { key, id, controller };
  try {
    const response = await fetch(String(track.url || ""), { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error(`Audio download failed (${response.status})`);
    const data = await response.arrayBuffer();
    if (controller.signal.aborted || generation !== configuredGeneration) return;
    if (data.byteLength !== size || data.byteLength > configuredBudget) throw new Error("Incomplete audio download");
    if (!await enforceBudget(data.byteLength)) return;
    await dbPut("audio", { key, data });
    try {
      await dbPut("entries", {
      key,
      trackId: id,
      generation,
      size: data.byteLength,
      modified,
      contentType: response.headers.get("Content-Type") || "application/octet-stream",
      lastAccess: Date.now()
      });
    } catch (error) {
      await dbDelete("audio", key);
      throw error;
    }
    const stale = (await dbGetAll("entries")).filter(entry => entry.trackId === id && entry.key !== key);
    for (const entry of stale) await removeEntry(entry);
  } finally {
    if (activeDownload?.key === key) activeDownload = null;
  }
}

async function cacheTracks(tracks) {
  const normalized = Array.isArray(tracks) ? tracks : [];
  desiredKeys = new Set(normalized.map(track => `${String(track.id || "")}:${Number(track.size || 0)}:${Number(track.modified || 0)}`));
  if (activeDownload && !desiredKeys.has(activeDownload.key)) activeDownload.controller.abort();
  for (const track of normalized) {
    try {
      await cacheOneTrack(track);
    } catch (error) {
      if (error?.name !== "AbortError") console.warn("Jukebox audio cache:", error);
    }
  }
  await enforceBudget(0);
  await broadcastStatus();
}

async function entryForTrack(trackId, generation) {
  if (!trackId || !generation) return null;
  const savedGeneration = String((await dbGet("settings", "generation"))?.value || "");
  if (savedGeneration !== generation) return null;
  const entries = await dbGetAll("entries");
  return entries.find(entry => String(entry.trackId) === trackId && entry.generation === generation) || null;
}

function parseRange(header, size) {
  if (!header || !header.startsWith("bytes=")) return null;
  const spec = header.slice(6).split(",", 1)[0].trim();
  let start = 0;
  let end = size - 1;
  if (spec.startsWith("-")) {
    const suffix = Number(spec.slice(1));
    if (!Number.isInteger(suffix) || suffix <= 0) return false;
    start = Math.max(0, size - suffix);
  } else {
    const [left, right] = spec.split("-", 2);
    start = Number(left);
    if (right) end = Number(right);
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end < start || start >= size) return false;
    end = Math.min(end, size - 1);
  }
  return { start, end };
}

async function cachedMediaResponse(request) {
  const url = new URL(request.url);
  const trackId = decodeURIComponent(url.pathname.slice("/media/".length));
  const generation = String(url.searchParams.get("generation") || "");
  const ticket = String(url.searchParams.get("ticket") || "");
  if (!configuredGeneration || generation !== configuredGeneration || !configuredTicket || ticket !== configuredTicket) {
    return fetch(request);
  }
  const entry = await entryForTrack(trackId, generation);
  if (!entry) return fetch(request);
  const stored = await dbGet("audio", entry.key);
  if (!(stored?.data instanceof ArrayBuffer)) {
    await removeEntry(entry);
    return fetch(request);
  }
  const blob = new Blob([stored.data], { type: entry.contentType || "application/octet-stream" });
  if (blob.size !== Number(entry.size || 0)) {
    await removeEntry(entry);
    return fetch(request);
  }
  entry.lastAccess = Date.now();
  dbPut("entries", entry).catch(() => {});
  const range = parseRange(request.headers.get("Range"), blob.size);
  const common = {
    "Content-Type": entry.contentType || blob.type || "application/octet-stream",
    "Accept-Ranges": "bytes",
    "Cache-Control": "private, no-store",
    "X-Jukebox-Audio-Cache": "hit"
  };
  if (range === false) {
    return new Response(null, { status: 416, headers: { ...common, "Content-Range": `bytes */${blob.size}` } });
  }
  if (range) {
    const body = blob.slice(range.start, range.end + 1, common["Content-Type"]);
    return new Response(body, {
      status: 206,
      headers: {
        ...common,
        "Content-Length": String(body.size),
        "Content-Range": `bytes ${range.start}-${range.end}/${blob.size}`
      }
    });
  }
  return new Response(blob, { status: 200, headers: { ...common, "Content-Length": String(blob.size) } });
}

self.addEventListener("message", event => {
  const message = event.data || {};
  if (message.type === "configure") event.waitUntil(configure(message));
  else if (message.type === "cache-tracks") {
    downloadChain = downloadChain.then(() => cacheTracks(message.tracks));
    event.waitUntil(downloadChain);
  } else if (message.type === "clear") event.waitUntil(clearStoredAudio(configuredGeneration));
  else if (message.type === "status") event.waitUntil(broadcastStatus());
});

self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method === "GET" && url.origin === self.location.origin && url.pathname.startsWith("/media/")) {
    event.respondWith(cachedMediaResponse(event.request).catch(() => fetch(event.request)));
  }
});
