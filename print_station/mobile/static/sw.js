'use strict';
// No fetch handler: private pages/status/photos are never cached for offline use.
self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('push', event => {
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch {}
  const ident = typeof payload.tag === 'string' && /^[a-f0-9]{32}$/.test(payload.tag) ? payload.tag : '';
  event.waitUntil(self.registration.showNotification(payload.title || 'Print Station', {
    body: payload.body || 'Open Print Station for details.',
    icon: '/static/icon.png', tag: ident || 'print-station',
    data: {url: ident ? '/?event=' + ident : '/'}
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const path = event.notification.data && event.notification.data.url;
  const safe = typeof path === 'string' && /^\/\?event=[a-f0-9]{32}$/.test(path) ? path : '/';
  const url = new URL(safe, self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const window of windows) {
      if (new URL(window.url).origin === self.location.origin) {
        await window.navigate(url);
        return window.focus();
      }
    }
    return self.clients.openWindow(url);
  })());
});
self.addEventListener('pushsubscriptionchange', event => {
  event.waitUntil((async () => {
    try {
      const session = await fetch('/api/session', {credentials: 'same-origin', cache: 'no-store'});
      if (!session.ok) return;
      const {csrf} = await session.json();
      const response = await fetch('/api/push', {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) return;
      const info = await response.json();
      if (!info.enabled) return;
      const raw = atob(info.public_key.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4-info.public_key.length%4)%4));
      const key = Uint8Array.from(raw, c => c.charCodeAt(0));
      const subscription = event.newSubscription || await self.registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key});
      await fetch('/api/push/subscribe', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type':'application/json', 'X-CSRF-Token':csrf}, body:JSON.stringify(subscription)});
    } catch {}
  })());
});
