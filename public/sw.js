// Minimal service worker – required by Chrome for PWA standalone installation
// Camera streams must not be intercepted; let the browser handle them directly.
self.addEventListener("fetch", (e) => {
  if (e.request.url.includes("/api/camera/")) return;
  e.respondWith(fetch(e.request));
});

self.addEventListener("push", (e) => {
  let data = { title: "Spooler", body: "" };
  try { data = e.data.json(); } catch {}
  e.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: "/icon.svg",
      badge: "/icon.svg",
    })
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(clients.openWindow("/"));
});

function _b64ToUint8Array(b64) {
  const pad = "=".repeat((4 - b64.length % 4) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

// Automatically renew an expired push subscription without user interaction.
self.addEventListener("pushsubscriptionchange", (e) => {
  e.waitUntil((async () => {
    try {
      const keyResp = await fetch("/api/push-public-key");
      if (!keyResp.ok) return;
      const { publicKey } = await keyResp.json();
      const sub = await self.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: _b64ToUint8Array(publicKey),
      });
      await fetch("/api/push-subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(sub.toJSON()),
      });
    } catch (err) {
      console.warn("[SW] Push resubscribe failed:", err);
    }
  })());
});
