// Minimal service worker – required by Chrome for PWA standalone installation
self.addEventListener("fetch", (e) => e.respondWith(fetch(e.request)));
