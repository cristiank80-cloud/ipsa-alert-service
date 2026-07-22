# IPSA Alert Service

Servicio que monitorea acciones IPSA cada cierto intervalo y te avisa por
correo y notificación push cuando una acción cae bajo su promedio
histórico de 90 días.

**Esto NO corre dentro de un chat con Claude.** Es un programa Python que
tiene que estar corriendo de forma continua en algún servidor (ver
sección de Hosting) para que funcione 24/7.

## Fuente de datos: Yahoo Finance (sin API key)

Este proyecto usa la librería `yfinance` para leer los precios de las
acciones IPSA (sufijo `.SN`, ej. `SQM-B.SN`). No requiere pedir acceso
a nadie ni esperar aprobación — funciona apenas instalas las
dependencias.

**Sé honesto contigo sobre el rezago:** Yahoo Finance etiqueta estas
cotizaciones como "Delayed Quote" y no publica cuántos minutos exactos
de rezago tienen. Es gratis e inmediato, pero no es garantizado como el
"tiempo real" de una API oficial de la Bolsa o de tu corredora. Si más
adelante consigues una fuente con menor rezago, solo hay que reemplazar
la función `get_quotes()` en `data_source.py` — el resto del servicio
(cálculo de promedio, alertas, notificaciones) no cambia.

**No pude probar las llamadas reales a Yahoo Finance desde este
entorno** (el dominio no está habilitado en mi sandbox de red) — sí
verifiqué que los 8 tickers existen en Yahoo Finance con el sufijo
`.SN`, pero corre `python main.py` una vez tú para confirmar que todo
responde como se espera antes de dejarlo corriendo en piloto automático.

## Qué necesitas antes de partir

1. **Nada para la fuente de datos** — Yahoo Finance no pide API key.
2. **Contraseña de aplicación de Gmail** (u otro proveedor SMTP) para el
   envío de correos. En Gmail: Cuenta Google → Seguridad → Verificación
   en 2 pasos → Contraseñas de aplicaciones.
3. **Par de claves VAPID** para las notificaciones push. Se generan una
   sola vez con:
   ```
   pip install py-vapid
   vapid --gen
   ```
   Esto crea `private_key.pem` / `public_key.pem`. La clave pública va
   en la PWA (ver más abajo), la privada va en `VAPID_PRIVATE_KEY`.

## Instalación local (para probar)

```bash
cd ipsa-alert-service
python -m venv venv
source venv/bin/activate   # en Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # y completa los valores
python main.py
```

## Notificaciones push en la PWA — ya implementado

El botón "🔔 Activar notificaciones" y el `sw.js` ya están escritos e
insertados en `ipsa_monitor_prototipo_v2.html` y en `sw.js` (carpeta
raíz de outputs), con tu clave pública VAPID ya puesta. Solo te falta
un paso manual: reemplazar `BACKEND_URL` en el HTML por la URL real de
tu servidor Render (ver `DEPLOY.md`, paso 6).

Nota: los service workers y Web Push requieren que la página se sirva
por **HTTPS** (o localhost) — no funciona abriendo el HTML directo
desde el disco (`file://`). Por eso también necesitas alojar la PWA en
algún lugar con HTTPS, no solo el backend.

## Hosting — ver DEPLOY.md

La guía completa y actualizada de despliegue está en `DEPLOY.md`. En
resumen: `main.py` corre vía **GitHub Actions** (gratis, cada 10
minutos) en vez de un proceso siempre encendido — Render no permite
Background Workers gratis, así que evitamos esa opción. `server.py`
(que recibe las suscripciones push) sí va en un Web Service gratuito de
Render, que es lo único que ese plan gratuito soporta.

## Limitaciones conocidas

- El promedio de 90 días se construye con los datos que este servicio
  va registrando día a día — el primer día no tendrás 90 días reales de
  historia. `yfinance` también puede entregar precios históricos
  (`Ticker.history(period="90d")`), así que se puede sembrar
  `price_history.json` con esos datos antes de partir en vez de esperar
  90 días — avísame si quieres que agregue ese script.
- Yahoo Finance es gratuito y no oficial para este uso: no hay garantía
  de disponibilidad ni de un rezago fijo. Si esto pasa a ser algo de lo
  que dependes para decisiones reales de inversión, vale la pena
  eventualmente migrar a una fuente con acuerdo formal (Bolsa de
  Santiago o tu corredora).
- Esto te avisa cuando algo cae bajo el promedio. **No es una
  recomendación de compra ni venta** — la decisión de invertir sigue
  siendo tuya; no soy asesor financiero y esta herramienta solo entrega
  información.
