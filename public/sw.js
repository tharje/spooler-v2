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
