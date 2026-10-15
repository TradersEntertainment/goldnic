/* Gold Nicomedia OS — servis çalışanı (sürüm {{ version }}).
 * Statik dosyalar: önce önbellek. Sayfalar: önce ağ, bağlantı yoksa çevrimdışı sayfası.
 * Kişisel veri içeren sayfalar ve API yanıtları ÖNBELLEĞE ALINMAZ (KVKK). */
const VERSION = '{{ version }}';
const STATIC_CACHE = `gn-static-${VERSION}`;
const PRECACHE = {{ precache|tojson }};
const OFFLINE_URL = '{{ offline_url }}';

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('gn-') && k !== STATIC_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res.ok && res.type === 'basic') {
          const copy = res.clone();
          caches.open(STATIC_CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }))
    );
    return;
  }

  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(() => caches.match(OFFLINE_URL)));
  }
});
