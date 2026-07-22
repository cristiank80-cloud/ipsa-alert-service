# Guía de configuración final

**Corrección importante respecto a lo que te dije antes:** Render NO
permite Background Workers en su plan gratuito (solo desde $7/mes). En
vez de eso, uso **GitHub Actions** como el "reloj" que ejecuta el
chequeo cada 10 minutos — es gratis de verdad, sin sorpresas de cobro,
y es un patrón bien establecido para tareas programadas livianas.

Arquitectura final:
- **GitHub Actions** (gratis) → corre `main.py` cada 10 minutos, revisa
  las 8 acciones, manda correo/push si hay alerta.
- **Render Web Service** (gratis) → solo recibe la suscripción push
  cuando activas notificaciones desde el celular. Se "duerme" si nadie
  lo usa por 15 minutos, pero eso no importa porque solo lo usas una
  vez al activar notificaciones (el primer request tarda unos segundos
  más, nada grave).

Sigue estos pasos en orden.

---

## 1. Contraseña de aplicación de Gmail (para el correo)

1. Ve a [myaccount.google.com/security](https://myaccount.google.com/security)
2. Activa "Verificación en 2 pasos" si no la tienes activada (obligatorio
   para el siguiente paso).
3. Busca "Contraseñas de aplicaciones".
4. Crea una nueva, ponle un nombre como "IPSA Monitor", copia la
   contraseña de 16 caracteres que te da (sin espacios).

---

## 2. Claves VAPID (para el push) — YA HECHO

Ya generé el par de claves, están en `vapid_keys.txt` e insertadas en
`ipsa_monitor_prototipo_v2.html`. Solo necesitas copiar el valor de
`VAPID_PRIVATE_KEY` a los "secrets" de GitHub más abajo.

---

## 3. Sube el código a GitHub

1. Crea una cuenta en [github.com](https://github.com) si no tienes.
2. Crea un repositorio nuevo. **Recomendación: hazlo privado.** Así no
   tienes que preocuparte de filtrar accidentalmente `vapid_keys.txt`
   o alguna clave si se te olvida excluir un archivo.
3. Sube todos los archivos de esta carpeta, incluyendo la carpeta
   `.github/workflows/monitor.yml` (mantén esa estructura).

## 4. Configura los "Secrets" en GitHub (tus claves, protegidas)

1. En tu repositorio → Settings → Secrets and variables → Actions.
2. "New repository secret" y agrega cada uno de estos, uno por uno:

| Nombre | Valor |
|---|---|
| `EMAIL_FROM` | tu correo Gmail |
| `EMAIL_APP_PASSWORD` | la contraseña de 16 caracteres del paso 1 |
| `EMAIL_TO` | a dónde quieres que lleguen las alertas (puede ser el mismo) |
| `VAPID_PRIVATE_KEY` | el bloque completo de `vapid_keys.txt`, con los saltos de línea |
| `VAPID_CLAIMS_EMAIL` | `mailto:tu_correo@gmail.com` |
| `SUBSCRIPTIONS_URL` | la URL de tu servidor Render + `/subscriptions` (la tendrás después del paso 5, puedes completar este secret después) |

3. Ve a la pestaña "Actions" de tu repositorio, deberías ver el
   workflow "Monitoreo IPSA". Puedes correrlo manualmente con el botón
   "Run workflow" para probarlo antes de esperar a que el cron lo
   dispare solo.

## 5. Despliega el servidor de suscripciones en Render

1. Ve a [render.com](https://render.com), crea cuenta (puedes usar tu
   cuenta de GitHub directo).
2. "New +" → **"Web Service"** (no Background Worker).
3. Conecta tu repositorio.
4. Configuración:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python server.py`
5. No necesita variables de entorno especiales (Render define `PORT`
   automáticamente).
6. Al desplegar, Render te da una URL tipo
   `https://ipsa-alert-service.onrender.com`. Guárdala.
7. Vuelve a GitHub Secrets y completa `SUBSCRIPTIONS_URL` con esa URL
   + `/subscriptions`, ej:
   `https://ipsa-alert-service.onrender.com/subscriptions`

## 6. Conecta la PWA con tu servidor real

1. Abre `ipsa_monitor_prototipo_v2.html` en un editor de texto.
2. Busca:
   ```javascript
   const BACKEND_URL = 'https://TU-SERVIDOR.onrender.com';
   ```
3. Reemplaza con tu URL real de Render del paso 5.
4. Sube `ipsa_monitor_prototipo_v2.html` **junto con `sw.js`** (misma
   carpeta) a un hosting con HTTPS — puedes usar un "Static Site" de
   Render (gratis, no se duerme porque es contenido estático) o GitHub
   Pages.
5. Abre esa URL desde tu celular, toca "🔔 Activar notificaciones",
   acepta el permiso.

---

## Cómo probar que el ciclo completo funciona

1. En GitHub → Actions → "Monitoreo IPSA" → "Run workflow" (manual).
2. Revisa los logs de esa ejecución — deberías ver algo como
   "Ejecutando un ciclo único · 8 acciones" y, si alguna acción está
   bajo su promedio, un "[ALERTA]".
3. Si configuraste bien el correo, deberías recibir un email de prueba
   apenas alguna acción cruce el umbral (o espera a que realmente pase,
   revisando los logs cada 10 minutos).

## Notas honestas sobre límites

- El cron de GitHub Actions **no es exacto al minuto** — GitHub puede
  retrasar la ejecución algunos minutos en momentos de alta demanda de
  su infraestructura. Para este uso (revisar acciones cada ~10 min) no
  debería ser un problema real.
- GitHub Actions en repos privados tiene minutos gratis limitados por
  mes (varía según el plan de tu cuenta) — revisa tu uso en Settings →
  Billing si te preocupa. Con este workflow (corridas cortas cada 10
  min, solo en horario de mercado) debería estar cómodamente dentro del
  límite gratuito para uso personal.
