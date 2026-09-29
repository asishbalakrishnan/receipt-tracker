// Keeps the app shell available offline and receives Android "Share to Receipts".
// API responses are never cached: receipts are private and must always come from the server.
const SHELL = "rt-shell-v1";
const SHARE = "share-target";

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.add("/")).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL && k !== SHARE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;
  if (e.request.method === "POST" && url.pathname === "/share") {
    e.respondWith(receiveShare(e.request));
  } else if (e.request.mode === "navigate" && url.pathname === "/") {
    e.respondWith(fetch(e.request).then((res) => { const copy = res.clone(); caches.open(SHELL).then((c) => c.put("/", copy)); return res; })
      .catch(() => caches.match("/")));
  }
});

async function receiveShare(request) {
  const form = await request.formData();
  const cache = await caches.open(SHARE);
  for (const k of await cache.keys()) await cache.delete(k);
  let i = 0;
  for (const file of form.getAll("files")) {
    if (!(file instanceof File)) continue;
    await cache.put(`/shared/${i++}`, new Response(file, { headers: { "content-type": file.type, "x-name": encodeURIComponent(file.name) } }));
  }
  return Response.redirect("/?shared=1", 303);
}
