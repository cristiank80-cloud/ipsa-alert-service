// Service worker mínimo: recibe el push del backend y lo muestra como
// notificación nativa del sistema, aunque la PWA esté cerrada.
self.addEventListener('push', event => {
  let data = { title: 'IPSA Monitor', body: 'Hay una novedad en tus alertas.' };
  try {
    data = event.data.json();
  } catch (e) {
    // si no viene como JSON, usamos el texto plano
    data.body = event.data ? event.data.text() : data.body;
  }
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: undefined,
      tag: data.ticker || 'ipsa-alert',
      // El backend manda 'url' (ver notify.py) con la tarjeta exacta a la
      // que apunta esta alerta. Antes esto no viajaba, asi que tocar la
      // notificacion solo abria la app en la pantalla principal -- habia
      // que buscar la accion a mano entre las 47.
      data: { url: data.url || '/' },
    })
  );
});

// Al tocar la notificación, va directo al detalle de la accion (no solo
// enfoca o abre la app en la pantalla principal).
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window' }).then(clientList => {
      for (const client of clientList) {
        if ('navigate' in client && 'focus' in client) {
          return client.navigate(url).then(c => c.focus());
        }
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
