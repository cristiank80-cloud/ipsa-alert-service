# IPSA Monitor — diagnóstico y parches v3

27 de julio de 2026 · revisión del kit `Kit Ipsa vf.zip`

---

## 1. Por qué el IPSA se quedó congelado

Tu app mostraba **10.888** a las 17:09. Banchile mostraba **10.964,11** al cierre de las 16:00.
Encontré tres fallas que se combinan, en orden de importancia:

### 1.1 `fast_info.last_price` nunca fue el precio en vivo

`data_source.py` pedía el precio así:

```python
info = yf.Ticker("^IPSA").fast_info
price = info.get("lastPrice")
```

Por dentro, yfinance resuelve esa propiedad así (lo verifiqué en el código de la librería,
yfinance 1.5.2):

```python
prices = self._tkr.history(period="1y", auto_adjust=False, keepna=True)
self._last_price = float(prices["Close"].iloc[-1])
```

Es **el último cierre diario**, no la cotización en vivo. Para acciones el bar del día se va
actualizando y el número se parece al precio real. Para el índice `^IPSA`, Yahoo publica el bar
diario tarde — por eso el índice se quedaba pegado y las 47 acciones no.

### 1.2 El timestamp mentía sobre la frescura

```python
"timestamp": datetime.now().isoformat()
```

Eso es la hora en que **el servidor pidió** el dato, no la hora a la que **corresponde** el precio.
Un cierre de hace tres días se veía exactamente igual de fresco que uno de hace un minuto. Por eso
no tenías forma de darte cuenta: el sistema no era capaz de decirte que estaba desactualizado.

### 1.3 Yahoo te estaba cortando por exceso de peticiones

Cada refresco de estadísticas disparaba **~190 peticiones secuenciales** a Yahoo:

| Función | Peticiones | Costo |
|---|---|---|
| `get_quotes` | 47 | 1 año de historial diario por acción |
| `get_bid_ask` | 47 | `.info` = scrape completo de la ficha, muy pesado |
| `get_daily_avg` | 47 | otro año de historial |
| `get_returns` | 47 | otro año más |
| `get_index_quote` | 1 | **el último de la fila** |

Yahoo responde `429 Too Many Requests` mucho antes de terminar, y yfinance se traga el error
devolviendo vacío. El índice, por pedirse al final, era **el primero en caerse**.

Y cuando `get_index_quote()` devolvía `None`, el backend mandaba `"index": null`, el frontend se
quedaba con el valor viejo de su caché, lo volvía a pintar **sin ninguna marca** — y el semáforo
seguía en verde diciendo *"DATOS REALES (47/47)"* porque las acciones sí habían llegado.

> **Cómo confirmarlo en 30 segundos:** despliega el parche y abre `/diag`. Ahí aparece
> `indice.hora_bolsa` y `indice.antiguedad_seg`. Si el valor sigue distinto al de Banchile pero la
> antigüedad es de minutos, entonces no es rezago: es que Yahoo publica la variante *price return*
> del índice y Banchile la *total return con dividendos* (fíjate que su ficha dice literalmente
> "S&P/CLX IPSA CLP TR (con dividendos)"). Son dos series distintas y nunca van a coincidir. No
> pude verificar cuál publica Yahoo porque no tengo acceso a su API desde acá.

### Lo que cambié

`data_source.py` está reescrito sobre el endpoint *chart* de Yahoo (público, sin API key):

- El precio ahora sale de `regularMarketPrice`, que **sí** es la cotización en vivo.
- Viaja `marketTime` (hora real de la bolsa) y `staleSeconds` en cada precio.
- **El índice se pide en la misma tanda paralela que las acciones**, no al final.
- De ~190 peticiones secuenciales a **48 en paralelo** (~2 s) para lo intradía y **48 más** cada
  30 min para todo lo histórico. Promedio, rentabilidad, RSI, volatilidad y la serie del gráfico
  salen ahora de **una sola** descarga por acción.
- `get_bid_ask` eliminado: 47 peticiones pesadas cada 3 minutos para mostrar "no disponible",
  porque Yahoo casi nunca publica puntas de la Bolsa de Santiago. Era el mayor consumidor de tu
  cuota y no aportaba nada.

En el frontend, el índice ahora se atenúa y muestra ⚠︎ cuando el dato tiene más de 15 minutos o
cuando el servidor no lo pudo obtener.

---

## 2. Por qué no te llegaron notificaciones

No puedo confirmarlo sin los logs de Render, pero hay **tres candidatos** y el parche arregla los
tres. Ordenados por probabilidad:

### 2.1 El fallo silencioso de `/run-check` (el más probable)

```python
prices = get_quotes(TICKERS)          # devuelve {} si Yahoo dio 429
for t in TICKERS:
    if t not in prices: continue      # no entra nunca
...
return jsonify({"checked": 0, ...})   # ← HTTP 200. Todo bien, aparentemente.
```

Si Yahoo te cortaba, el endpoint respondía **200 OK** con cero alertas. Tu cron externo veía un
200 y quedaba conforme. Nadie se enteraba de nada. Esto es exactamente compatible con lo que
pasó: el índice congelado (§1.3) y las notificaciones ausentes son **el mismo problema**.

**Ahora:** devuelve **HTTP 503** cuando no hay datos, y tras 3 ciclos fallidos seguidos te manda
un correo de alarma. Un monitor externo sobre `/run-check` ahora sí te avisa.

### 2.2 Las suscripciones push se borran solas

El disco de Render en plan gratuito es **efímero**: `push_subscriptions.json` desaparece en cada
reinicio y en cada despliegue.

Tu código ya tenía `resincronizarSuscripcion()`, pero **solo corría al abrir la app**. Si el
servidor se reinició en la mañana y no abriste la app hasta las 17:08 (la hora de tu captura), el
servidor pasó todo el día sin saber a quién avisarle.

Peor: si ese re-registro fallaba, el error se tragaba en el `catch` y el botón seguía en verde —
porque *"notificaciones activas"* salía de `localStorage` del celular, no del servidor. Ese falso
verde es literalmente lo que muestra tu captura.

**Ahora:** el re-registro también corre al volver a la app y cada 30 min, y el botón pasa a
**⚠︎ Notificaciones sin confirmar** si el servidor no responde. Además `/diag` te dice cuántos
dispositivos tiene registrados.

### 2.3 El silencio era ambiguo

El sistema solo sabía hablar cuando algo cruzaba el umbral. No había forma de distinguir
*"hoy no cruzó nada"* de *"esto lleva días roto"*.

**Ahora:** existe `/resumen-diario`, un correo que llega **todos los días hábiles, haya o no
alertas**. Si un día no llega, ya sabes que hay que revisar. Prográmalo en tu cron después del
cierre.

---

## 3. Sobre las propuestas de compra y venta

Te construí la capa de señales (`signals.py`), pero quiero ser claro sobre el límite:

**No te voy a decir "compra X, vende Y".** No soy asesor financiero, no conozco tu horizonte ni tu
tolerancia al riesgo, y sobre todo: con precios que vienen de Yahoo con rezago no declarado,
cualquier recomendación puntual sería irresponsable. Tu propia app ya lo dice bien en el panel de
análisis — mantuve ese criterio.

Lo que sí te sirve es hacer **explícitas y auditables** las reglas que hoy están implícitas, con
el número y la razón al lado, para que puedas discutirle al modelo en vez de creerle.

---

## 4. Los cuatro defectos del modelo actual (y cómo quedaron)

La regla completa era `precio < promedio_90d × 0,96`. Un solo umbral, igual para todos.

### 4.1 Un -4% fijo para acciones con volatilidad muy distinta

−4% en AGUAS-A (se mueve ~0,8% al día) es un evento raro. −4% en ENJOY (se mueve ~5% al día) es un
martes cualquiera. **Por eso tu panel se llena siempre de las mismas acciones chicas y volátiles:**
ENJOY −23,4%, ORO-BLANCO −7,2%, BLUMAR −6,3%. El modelo no estaba detectando oportunidades, estaba
detectando volatilidad.

→ Reemplazado por **z-score**: a cuántas desviaciones estándar de *su propio* promedio está cada
acción. Cada una compite contra sí misma.

### 4.2 No distinguía caída del mercado de caída de la acción

Si el IPSA cae 3%, caen 40 acciones juntas y te llegan 16 alertas que dicen todas lo mismo. Eso no
es información, es ruido correlacionado.

→ Se mide la **fuerza relativa**: retorno de la acción menos retorno del índice en el mismo
período. Si se quedó 10+ puntos atrás del IPSA, se marca como bandera roja — está cayendo por algo
suyo, y conviene buscar la noticia antes de asumir que está barata.

### 4.3 No distinguía un retroceso sano de una caída libre

Comprar barato lo que sigue bajando no es comprar barato.

→ Filtro de tendencia con SMA20/SMA50. Si el precio está bajo la media de 50 **y** la de 20 va por
debajo de la de 50, se marca **CAIDA SOSTENIDA** y se degrada la clasificación, aunque el puntaje
sea alto.

### 4.4 No miraba liquidez

Una señal perfecta en una acción que transa $20 millones al día no se puede ejecutar sin moverte
el precio en contra.

→ Filtro de monto transado promedio (30 días). Bajo $150 M/día el puntaje se reduce a la mitad y
la acción sale del ranking.

### Prueba del motor con casos sintéticos

| Caso | Puntaje | Clasificación |
|---|---|---|
| Retroceso dentro de tendencia alcista, líquida, RSI 29 | **+59** | `revisar_compra` |
| −11,5% bajo el promedio, RSI 27, **pero en caída sostenida y 20 pts atrás del IPSA** | **+16** | `neutro` |
| −31% bajo el promedio, RSI 24, **pero transa $18 M/día** | **+10** | `neutro` (3 banderas) |
| +9,6% sobre el promedio, RSI 78 | **−52** | `revisar_venta` |

Fíjate en la segunda fila: **eso es exactamente lo que la regla vieja te habría gritado como la
mejor oportunidad del día.**

---

## 5. Otros arreglos incluidos

**El RSI nunca funcionó.** Se calculaba en el celular sobre los precios que llegaban cada 60
segundos, y `hydrateFromCache` reseteaba el arreglo a un solo punto en cada recarga. RSI(14)
necesita 15 puntos: te habrías tenido que quedar 15 minutos con la app abierta y sin recargarla.
Por eso las 47 tarjetas dicen *"RSI: acumulando"* en tu captura. Ahora viene del backend, calculado
sobre cierres diarios de un año, con el método de Wilder (el anterior usaba promedio simple, que da
un número distinto al de cualquier plataforma contra la que lo compares).

**Precios ajustados por dividendo.** Todas las estadísticas usan `adjclose`. En Chile los
dividendos son altos: sin ajustar, la caída mecánica del día ex-dividendo se confunde con una
caída real y dispara una alerta falsa.

**Suscripciones deduplicadas por `endpoint`** en vez de por objeto completo (comparar el objeto
fallaba si cambiaba el orden de las claves del JSON).

**`/email-test`** — faltaba el equivalente de `/push-test` para el correo.

**`/diag`** — todo el estado de salud en una pantalla: edad de cada caché, si el índice está
disponible y de cuándo es, cuántos dispositivos push hay registrados, si el correo está
configurado, cuántos chequeos seguidos fallaron.

---

## 6. Qué hacer ahora

1. **Backend** — reemplaza en tu repo: `data_source.py`, `server.py`, `notify.py`, y agrega
   `signals.py`. Redespliega en Render.
2. **Frontend** — reemplaza `ipsa_monitor_prototipo_v2.html`.
3. **Abre la app en el celular** una vez, para que se vuelva a registrar el push.
4. **Verifica** en este orden:
   - `/diag?token=TU_SECRET` → `suscripciones_push` debe ser ≥ 1, `indice.disponible` en `true`
   - `/email-test?token=...` → debe llegarte un correo
   - `/push-test?token=...` → debe llegarte una notificación
   - `/signals` → el ranking con sus razones
5. **Agrega al cron** una llamada diaria a `/resumen-diario?token=...` después del cierre. Ese es
   tu latido: si un día no llega ese correo, el servicio está roto.

### Ajustes que quedan a tu criterio

Todos los umbrales están arriba de `signals.py` y se pueden cambiar por variable de entorno sin
tocar código: `Z_COMPRA_ATENCION`, `RSI_SOBREVENTA`, `LIQUIDEZ_MINIMA_CLP`, `FR_DIVERGENCIA`.
El modo de alerta se elige con `ALERTA_MODO=z` (nuevo, por defecto) o `ALERTA_MODO=pct` (el −4% de
antes).

### Dos cosas que no pude verificar y conviene que revises

- **El horario de cierre.** El código asume 09:30–16:00. Tu captura de Banchile es consistente con
  eso para julio, pero el horario de la Bolsa de Santiago cambia con el horario de verano. Revísalo.
- **`FERIADOS_2026`** está escrito a mano en el HTML y hay que actualizarlo cada año.

---

*Nada de este documento es asesoría financiera. Los precios provienen de Yahoo Finance con rezago
no declarado — verifica siempre en tu corredora antes de operar.*

---

# Anexo — segunda ronda

## 7. Fecha y hora del dato en cada tarjeta

Sí, y era justo lo que faltaba. El backend ya venía mandando `marketTime` (la hora **de la
bolsa**, no la hora en que la app consultó) y `staleSeconds`; ahora se muestran en tres lugares:

**En cada tarjeta**, bajo el "Promedio 90d":

```
Promedio 90d                    337
27/07/2026 · 15:58        hace 4 min
```

**Junto al IPSA**, en la barra superior:

```
10.964   +0,12%   27/07/2026 · 16:00 · hace 3 min
```

**En el detalle**, una fila nueva: *Fecha y hora del dato → 27/07/2026 · 15:58 (hace 4 min)*.

Con semáforo por antigüedad: gris hasta 30 min, dorado entre 30 min y 6 h, **rojo con ⚠︎ sobre
6 h**. El umbral rojo es de horas y no de minutos a propósito: con el mercado cerrado es normal
que el dato sea del cierre, y no tendría sentido alarmarte todas las tardes.

Si el índice hubiera tenido esta línea, el problema de ayer se habría visto solo.

## 8. Salir de Yahoo: la API oficial de la Bolsa de Santiago

Sí se puede, y es la respuesta correcta a tu pregunta.

La Bolsa de Comercio de Santiago tiene una **API para desarrolladores** en
[startup.bolsadesantiago.com](https://startup.bolsadesantiago.com). Hay que **solicitar una API
key** al equipo que la mantiene. Existe además un SDK en Python (`pip install bolsa-stgo`,
[LautaroParada/bolsa-santiago](https://github.com/LautaroParada/bolsa-santiago)) que documenta
bien los endpoints.

Lo que entrega y Yahoo no:

| Endpoint | Qué trae |
|---|---|
| `get_indices_rv` | valor de los índices, variación y volumen — **el IPSA directo de la fuente** |
| `get_instrumentos_rv` | apertura, máximo, mínimo y volumen de **todo el mercado en una sola llamada** |
| `get_puntas_rv` | **puntas de compra y venta** — el dato que Yahoo simplemente no tiene para Chile |
| `get_transacciones_rv` | últimas transacciones ejecutadas |
| `get_resumen_accion` | ficha detallada por nemotécnico |

Te dejé el adaptador escrito y conectado: **`fuente_bolsa.py`**.

- Si defines `BOLSA_API_KEY` en Render, el servidor usa la Bolsa de Santiago automáticamente.
- Si esa fuente falla (cuota agotada, clave vencida, caída), **cae de vuelta a Yahoo solo**, en
  vez de dejarte sin datos.
- Si no defines la variable, todo sigue exactamente como ahora. No rompe nada.
- `/quotes` te dice en el campo `fuente` cuál se está usando en este momento.

**Dos advertencias honestas:**

1. **No pude verificar el costo ni las condiciones de uso.** La página es una aplicación
   JavaScript y no se deja leer desde acá. Tampoco sé si hay plan gratuito. Averígualo antes de
   apoyarte en esto. También tiene límite diario de peticiones (`get_request_usuario` te dice
   cuántas te quedan).
2. **Los nombres de los campos en `fuente_bolsa.py` están escritos según la documentación del
   SDK, no probados contra la API real** — no tengo la clave. Cuando la consigas, llama primero a
   `/diag-bolsa?token=...`: te devuelve la respuesta cruda para que ajustes el diccionario
   `_MAPA_CAMPOS`, que está todo junto arriba del archivo. Preferí dejar las adivinanzas
   explícitas y en un solo lugar antes que repartirlas por el código.

### Antes de pagar por datos: comprueba si de verdad los necesitas

Tu queja concreta fue el **índice**, no las acciones. Son dos cosas distintas:

- **Las acciones** con rezago de ~15-20 min probablemente te sirven, si no estás operando
  intradía. Con la línea de fecha/hora nueva vas a poder confirmarlo en un vistazo.
- **El índice** puede estar mal por otra razón que no es rezago: Banchile muestra
  *"S&P/CLX IPSA CLP **TR** (con dividendos)"* y Yahoo podría estar publicando la variante
  *price return*. Si es eso, **son dos series distintas y nunca van a coincidir**, por muy en vivo
  que estén.

Despliega, abre `/diag` y mira `indice.antiguedad_seg`. Si dice minutos pero el número sigue sin
cuadrar con Banchile, es el problema de la serie, no del rezago — y ahí la API de la Bolsa sí lo
resuelve de raíz. Si dice horas o días, era rezago.

### Otras alternativas, para que tengas el mapa completo

- **Tu corredora.** Por tus marcadores veo que eres cliente de Banchile. Vale la pena preguntar si
  dan acceso a datos por API: sería la vía más barata y con derecho de uso claro.
- **Endpoints internos de bolsadesantiago.com o del sitio de Banchile.** Técnicamente se puede,
  pero no están documentados, cambian sin aviso y hay que revisar los términos de uso de cada
  sitio. No te lo recomiendo como base de algo que quieres que funcione solo.
- **Proveedores comerciales** (marketstack, Twelve Data, EODHD). Su cobertura de la Bolsa de
  Santiago es irregular; habría que verificar acción por acción antes de comprometerse.

## 9. Una cosa que vi en tu captura

Las seis tarjetas dicen **"Cargando datos reales…"** y el "Promedio 90d" está en `···`. Eso
significa que el frontend no está recibiendo respuesta de `/quotes`. Lo más probable es que Render
esté dormido (el plan gratuito tarda hasta ~50 s en despertar) o que estés viendo el HTML nuevo
sin haber desplegado todavía el backend nuevo. Si después de desplegar sigue igual, abre
`/health` y `/diag` directo en el navegador: eso separa "el servidor está caído" de "el servidor
está vivo pero sin datos".
