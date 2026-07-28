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
    })
  );
});

// Al tocar la notificación, enfoca o abre la PWA.
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window' }).then(clientList => {
      for (const client of clientList) {
        if ('focus' in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow('/');
    })
  );
});
