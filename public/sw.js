// Minimal service worker – required by Chrome for PWA standalone installation
// Camera streams must not be intercepted; let the browser handle them directly.
self.addEventListener("fetch", (e) => {
  if (e.request.url.includes("/api/camera/")) return;
  e.respondWith(fetch(e.request));
});
