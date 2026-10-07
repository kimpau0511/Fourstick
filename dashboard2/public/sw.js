// 화면 껍데기만 캐시하는 서비스 워커. 화면이 안 바뀌어 보이면 CACHE 버전을 올린다.
const CACHE = 'forstick-shell-v1';
const PRECACHE = ['/', '/manifest.webmanifest', '/icon.svg', '/icon-192.png', '/icon-512.png', '/icon-maskable-512.png'];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// 캐시 우선, 없으면 네트워크에서 받아 저장
const cacheFirst = async (request) => {
  const hit = await caches.match(request);
  if (hit) return hit;
  const res = await fetch(request);
  if (res.ok) (await caches.open(CACHE)).put(request, res.clone());
  return res;
};

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);
  // 제어 API(명령·승인·실행·정지)와 상태 값은 캐시하면 오래된 값이 '현재 상태'로 보인다 — 절대 캐시하지 않는다.
  // respondWith를 부르지 않아 브라우저 기본 네트워크로 간다. 웹소켓도 SW를 지나지 않는다.
  if (request.method !== 'GET' || url.origin !== self.location.origin
    || url.pathname.startsWith('/v1/') || url.pathname.startsWith('/health')) return;

  if (request.mode === 'navigate') {
    // 네트워크 우선: 새 build가 바로 보이고, 오프라인이면 캐시된 껍데기를 쓴다.
    event.respondWith(
      fetch(request)
        .then(async (res) => {
          if (res.ok) (await caches.open(CACHE)).put('/', res.clone());
          return res;
        })
        .catch(() => caches.match('/')),
    );
    return;
  }
  // /assets/(해시 파일)와 아이콘·매니페스트 등 같은 origin 정적 파일
  event.respondWith(cacheFirst(request));
});
