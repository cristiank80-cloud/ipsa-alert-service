# Cómo instalar los parches — paso a paso

Tiempo estimado: **20 minutos**. No hace falta programar: es subir archivos y copiar/pegar.

Tu sistema tiene dos partes separadas, y hay que actualizar las dos:

| Parte | Dónde vive | Qué la actualiza |
|---|---|---|
| Servidor (backend) | GitHub → Render | Parte B |
| App del celular (frontend) | GitHub → GitHub Pages | Parte C |

---

# ⚠️ PARTE A — Antes de tocar nada (5 min)

## A.1 Rescata tus dos valores personales

**Este es el paso que más se olvida y el que más rompe cosas.**

El archivo `ipsa_monitor_prototipo_v2.html` que te entregué trae las claves del kit original,
**no las tuyas**. Si lo subes tal cual, las notificaciones dejan de funcionar y la app apunta al
servidor equivocado.

Abre tu HTML **actual** (el que ya tienes funcionando, en tu repo `ipsa-app` de GitHub) y busca
estas dos líneas. En el Bloc de notas usa `Ctrl+B` para buscar:

```
const VAPID_PUBLIC_KEY = '...aquí va tu clave, 87 caracteres...';
const BACKEND_URL = 'https://TU-SERVICIO.onrender.com';
```

**Copia esos dos valores a un bloc de notas.** Los vas a necesitar en el paso C.2.

> Si no encuentras tu HTML actual: en GitHub entra a tu repo `ipsa-app`, click en el archivo
> `ipsa_monitor_prototipo_v2.html`, y ahí lo lees directo en pantalla.

## A.2 Haz una copia de seguridad

En GitHub no la necesitas — el historial guarda todo y siempre puedes volver atrás. Pero si te
deja más tranquilo, descarga los dos repos con **Code → Download ZIP** y guárdalos aparte.

## A.3 Ten a mano tu `CHECK_SECRET`

Es la palabra rara que inventaste al configurar Render (ej. `perro-azul-8291-xyz`). Si no la
recuerdas: Render → tu servicio → **Environment** → ahí aparece.

---

# PARTE B — Actualizar el servidor (7 min)

## B.1 Subir los archivos nuevos a GitHub

1. Entra a tu repo del backend en GitHub (el que se llama `ipsa-alert-service` o parecido).
2. Click **Add file** → **Upload files**.
3. Arrastra estos **5 archivos** desde la carpeta `parches-v3/backend/`:

   | Archivo | Qué es |
   |---|---|
   | `data_source.py` | reemplaza el tuyo |
   | `server.py` | reemplaza el tuyo |
   | `notify.py` | reemplaza el tuyo |
   | `requirements.txt` | reemplaza el tuyo |
   | `signals.py` | **nuevo** |
   | `fuente_bolsa.py` | **nuevo** |

   > **NO subas** `main.py`, `news.py`, `indicators.py` ni `alerts_engine.py`. Esos no
   > cambiaron. Están en la carpeta solo para que tengas el backend completo en un mismo lugar.
   > Tampoco subas `LEEME.txt` ni la carpeta `__pycache__` (esa es basura, bórrala).

4. Baja hasta abajo y click **Commit changes**.

GitHub te va a avisar que los archivos ya existen y los va a reemplazar. Eso es lo que queremos.

## B.2 Esperar el despliegue

Render detecta el cambio solo y redespliega. Entra a
[dashboard.render.com](https://dashboard.render.com) → tu servicio → pestaña **Logs**.

Espera a ver algo así:

```
==> Build successful 🎉
==> Starting service with 'gunicorn server:app'
```

Va a tardar **menos que antes** — sacamos `yfinance`, que arrastraba pandas y numpy.

**Si el build falla:** copia el error de los Logs y mándamelo. Lo más probable sería que falte
subir alguno de los archivos nuevos.

## B.3 Comprobar que el servidor quedó bien

Abre esto en el navegador, reemplazando la URL y el token por los tuyos:

```
https://TU-SERVICIO.onrender.com/diag?token=TU_CHECK_SECRET
```

Fíjate en tres cosas:

```json
"precios_en_cache": 47,          ← debe ser un número alto, no 0
"indice": {
   "disponible": true,           ← debe decir true
   "valor": 10964,
   "antiguedad_seg": 180         ← ESTE es el número clave
},
"correo_configurado": true
```

> **La primera vez puede tardar hasta 50 segundos** en responder: el plan gratuito de Render
> duerme el servidor y hay que esperar a que despierte. Si no carga, refresca y espera.

### 🔍 Aquí resuelves el misterio del IPSA

Compara `indice.valor` con lo que muestra Banchile en ese mismo momento:

- **`antiguedad_seg` en miles (horas o días)** → era rezago. Ya está arreglado, el valor se va a
  poner al día solo.
- **`antiguedad_seg` bajo (minutos) pero el número igual no cuadra con Banchile** → no es rezago:
  Yahoo publica una variante distinta del índice. Ahí conviene ir a la API de la Bolsa (Parte E).

## B.4 Probar correo y push

Uno a la vez, en el navegador:

```
https://TU-SERVICIO.onrender.com/email-test?token=TU_CHECK_SECRET
```
→ Debe responder `{"enviado": true}` y **llegarte un correo**. Revisa spam por si acaso.

```
https://TU-SERVICIO.onrender.com/push-test?token=TU_CHECK_SECRET
```
→ Va a decir `"suscripciones": 0`. **Es normal en este momento** — todavía no actualizas la app
del celular. Lo repetimos en el paso C.5.

---

# PARTE C — Actualizar la app del celular (5 min)

## C.1 Abrir el HTML nuevo

Abre `parches-v3/frontend/ipsa_monitor_prototipo_v2.html` con el **Bloc de notas** (click
derecho → Abrir con → Bloc de notas).

## C.2 ⚠️ Pegar TUS dos valores

Con `Ctrl+B` busca `VAPID_PUBLIC_KEY`. Vas a llegar cerca de la línea 2245:

```javascript
const VAPID_PUBLIC_KEY = 'BOQ6bcNgmXfK7oB9_Z2wca_1cd9oaqnebftUQf3hAkLX...';
const BACKEND_URL = 'https://ipsa-alert-service.onrender.com';
```

**Reemplaza los dos valores entre comillas por los tuyos** (los que rescataste en el paso A.1).

Cuidado con no borrar las comillas ni el punto y coma del final. Debe quedar así:

```javascript
const VAPID_PUBLIC_KEY = 'TU_CLAVE_PUBLICA_DE_87_CARACTERES';
const BACKEND_URL = 'https://TU-SERVICIO.onrender.com';
```

**Guarda el archivo** (`Ctrl+G` o Archivo → Guardar).

> Si te saltas este paso, el síntoma es el error `VapidPkHashMismatch` y las notificaciones
> nunca llegan.

## C.3 Subirlo a GitHub

1. Entra a tu repo del frontend (`ipsa-app` o como lo hayas llamado).
2. **Add file** → **Upload files**.
3. Arrastra **solo** `ipsa_monitor_prototipo_v2.html` (el que acabas de editar).
   Los otros 4 archivos (`manifest.json`, `sw.js`, los 2 iconos) **no cambiaron**.
4. **Commit changes**.

GitHub Pages tarda **1-2 minutos** en publicar el cambio.

## C.4 Refrescar la app en el celular

El celular tiene la versión vieja guardada en caché. Para forzar la actualización:

- **iPhone:** cierra la app por completo (deslizar hacia arriba desde el selector de apps) y
  vuelve a abrirla. Si sigue igual: Ajustes → Safari → Borrar historial y datos.
- **Android:** abre la app, menú ⋮ → Configuración del sitio → Borrar datos. O simplemente
  ciérrala del todo y vuelve a abrirla.

**Cómo sabes que agarró la versión nueva:** bajo el "Promedio 90d" de cada tarjeta ahora aparece
una línea con **fecha y hora**, así:

```
Promedio 90d              337
27/07/2026 · 15:58   hace 4 min
```

Y arriba, junto al IPSA, también sale la fecha y hora.

## C.5 Reactivar las notificaciones

1. En la app, toca **🔔 Activar notificaciones** (o **⚠︎ Notificaciones sin confirmar** si aparece
   así). Acepta el permiso.
2. Vuelve al navegador del computador:
   ```
   https://TU-SERVICIO.onrender.com/push-test?token=TU_CHECK_SECRET
   ```
3. Ahora debe decir `"enviados": 1` y **te llega una notificación al celular**.

Si dice `"enviados": 0` y `"suscripciones": 0`, la app no alcanzó a registrarse: cierra y abre la
app de nuevo, y repite.

---

# PARTE D — Programar el latido diario (3 min)

Esta es la pieza que hacía falta: **un correo que llega todos los días, haya o no alertas.** Si un
día no llega, sabes que algo se rompió — antes el silencio no significaba nada.

## D.1 Si ya usas cron-job.org

Agrega un **job nuevo** (no toques el que ya tienes para `/run-check`):

- **URL:** `https://TU-SERVICIO.onrender.com/resumen-diario?token=TU_CHECK_SECRET`
- **Horario:** todos los días a las **16:30** hora de Santiago (media hora después del cierre)
- **Días:** lunes a viernes

## D.2 Si no tienes cron todavía

Entra a [cron-job.org](https://cron-job.org), crea una cuenta gratis, y crea **dos** jobs:

| Job | URL | Cada cuánto |
|---|---|---|
| Chequeo de alertas | `https://TU-SERVICIO.onrender.com/run-check?token=TU_SECRET` | cada 10 min, 9:30–16:00, lun–vie |
| Resumen diario | `https://TU-SERVICIO.onrender.com/resumen-diario?token=TU_SECRET` | 16:30, lun–vie |

## D.3 Revisa tu UptimeRobot

El monitor que tienes apuntando a `/health` **déjalo tal cual** — sirve para que Render no se
duerma.

Si quieres además enterarte cuando la fuente de datos falle, agrega un segundo monitor a
`/run-check?token=TU_SECRET`. Ahora ese endpoint devuelve **error 503** cuando no hay datos (antes
devolvía 200 aunque estuviera todo roto), así que UptimeRobot te va a avisar de verdad.

---

# PARTE E — (Opcional) Datos directos de la Bolsa de Santiago

**Haz esto solo si el paso B.3 te mostró que Yahoo no te sirve.** Si los datos quedaron bien, sáltate
esta parte entera — no necesitas hacer nada más.

1. Entra a [startup.bolsadesantiago.com](https://startup.bolsadesantiago.com) y **solicita una API
   key**. Pregunta el costo y el límite de peticiones: no pude verificarlo y puede no ser gratis.
2. Cuando te la entreguen: Render → tu servicio → **Environment** → **Add Environment Variable**:
   - Key: `BOLSA_API_KEY`
   - Value: la clave que te dieron
   - **Save changes** (Render redespliega solo)
3. Comprueba:
   ```
   https://TU-SERVICIO.onrender.com/diag-bolsa?token=TU_CHECK_SECRET
   ```
4. **Mándame lo que devuelva ese link.** Los nombres de los campos los escribí según la
   documentación, sin poder probarlos contra la API real. Con esa respuesta ajusto el mapeo en
   10 minutos y quedan funcionando las puntas de compra/venta, que Yahoo nunca te dio.

Mientras tanto, si esa fuente falla, el servidor vuelve a Yahoo solo. No te quedas sin datos.

---

# Checklist final

- [ ] `/diag` muestra `precios_en_cache` alto y `indice.disponible: true`
- [ ] `/email-test` → te llegó el correo
- [ ] Las tarjetas de la app muestran **fecha y hora** bajo el promedio
- [ ] `/push-test` → `"enviados": 1` y te llegó la notificación
- [ ] Los dos jobs del cron creados
- [ ] Mañana a las 16:30 te llega el **resumen diario**

---

# Si algo sale mal

| Síntoma | Qué mirar |
|---|---|
| Todas las tarjetas dicen "Cargando datos reales…" | Abre `/health`. Si no responde, Render está dormido: espera 50 s. Si responde pero las tarjetas siguen vacías, mira `/diag` |
| `"precios_en_cache": 0` en `/diag` | Yahoo está rechazando las peticiones. Espera 10 min y vuelve a mirar; si sigue en 0, avísame |
| Notificaciones no llegan / `VapidPkHashMismatch` | Te saltaste el paso **C.2**. Verifica que el `VAPID_PUBLIC_KEY` del HTML sea el tuyo |
| El botón dice "⚠︎ Notificaciones sin confirmar" | La app no logra hablar con el servidor. Revisa que `BACKEND_URL` en el HTML sea tu URL real |
| La app se ve igual que antes | El celular tiene la caché vieja. Repite el paso **C.4** |
| El build de Render falla | Copia el error de los Logs y mándamelo |

---

*Ninguna parte de este sistema es asesoría financiera. Los precios vienen con rezago — verifica en
tu corredora antes de operar.*
