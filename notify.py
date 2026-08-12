"""
Envio de alertas por dos canales:
  - Correo, via la API de Resend (HTTPS, no SMTP -- Render y varios
    hostings gratuitos bloquean el puerto SMTP saliente, por eso se
    migro de smtplib a esto).
  - Push al celular, via Web Push (VAPID) hacia la PWA instalada.

NOTA SOBRE LA CLAVE VAPID
-------------------------
pywebpush recibe la clave privada como texto y la interpreta tal cual.
Si el valor guardado en la variable de entorno trae comillas, espacios,
saltos de linea escritos como texto ("\\n" en dos caracteres), cabeceras
PEM, o le falta/sobra un caracter, el error que aparece NO dice "clave
invalida" sino algo criptico:

    Invalid base64-encoded string: number of data characters (133)
    cannot be 1 more than a multiple of 4

Ese error viene de base64, no de la libreria de push, y ocurre ANTES de
contactar al servicio de notificaciones -- por eso falla identico para
las 47 acciones y no lo atrapa el "except WebPushException".

Aqui la clave se normaliza y se convierte UNA sola vez a un objeto
Vapid, aceptando cualquiera de estos formatos:
  - raw base64url de 32 bytes (43 caracteres, el formato tipico)
  - DER base64 (PKCS8 o SEC1)
  - PEM completo, con saltos de linea reales o escritos como "\\n"
"""
import os
import json
import base64
import binascii
import requests

from pywebpush import webpush, WebPushException
from py_vapid import Vapid01
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

# ---- Correo (API de Resend) ----
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
# El remitente "sandbox" de Resend no requiere verificar un dominio,
# pero SOLO puede mandar al correo con el que te registraste en Resend
# -- que es justo tu caso, te mandas las alertas a ti mismo.
RESEND_FROM = os.environ.get("RESEND_FROM", "onboarding@resend.dev")
EMAIL_TO = os.environ.get("EMAIL_TO")

# URL publica de la PWA (GitHub Pages). Se usa para armar el enlace "toca
# para ver el detalle" en el correo y en el push -- antes ninguno de los
# dos avisos llevaba a ningun lado en particular, solo abrian la app en la
# pantalla principal.
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://cristiank80-cloud.github.io/ipsa-app/")


def send_alert(ticker, direccion, precio, avg, cuerpo=None):
    """
    Correo de alerta BIDIRECCIONAL: 'compra' cuando el precio esta lejos
    bajo su propio promedio, 'venta' cuando esta lejos por arriba.

    Reemplaza al viejo send_email_alert(), que solo sabia avisar caidas
    (el nombre y el asunto decian "cayo" incluso conceptualmente para
    cualquier alerta). Ahora el texto se arma segun la direccion real de
    la senal, y siempre incluye un enlace directo a la tarjeta de esa
    accion en la app -- antes habia que ir a buscarla a mano entre 47.
    """
    if not (RESEND_API_KEY and EMAIL_TO):
        print("[notify] Correo no configurado (falta RESEND_API_KEY o EMAIL_TO), se omite envio.")
        return

    if direccion == "venta":
        emoji, palabra, prep = "📈", "posible venta", "sobre"
    else:
        emoji, palabra, prep = "📉", "posible compra", "bajo"

    dist_txt, cuerpo_precio = "", f"{ticker} está en {precio:,.0f}"
    if avg:
        dist = abs((precio / avg - 1) * 100)
        dist_txt = f" — {dist:.1f}% {prep} su promedio"
        cuerpo_precio += f", un {dist:.1f}% {prep} su promedio histórico de 90 días ({avg:,.0f})"
    cuerpo_precio += "."

    subject = f"{emoji} {ticker}: {palabra}{dist_txt}"
    body = cuerpo_precio + "\n\n"
    if cuerpo:
        body += cuerpo + "\n\n"
    body += f"👉 Toca aquí para ver el detalle en la app:\n{FRONTEND_URL}?ticker={ticker}"

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": RESEND_FROM,
                "to": [EMAIL_TO],
                "subject": subject,
                "text": body,
            },
            timeout=10,
        )
        if resp.status_code >= 400:
            print(f"[notify] Resend rechazo el correo ({resp.status_code}): {resp.text}")
        else:
            print(f"[notify] Correo enviado para {ticker} (via Resend)")
    except Exception as e:
        print(f"[notify] Error enviando correo via Resend: {e}")


def send_raw_email(subject, body):
    """
    Correo libre (asunto + texto), sin el formato de alerta de una accion.
    Lo usan el resumen diario y las alarmas de servicio caido.

    Faltaba: send_email_alert() obligaba a pasar ticker/precio/promedio, asi
    que no habia forma de mandar un mensaje del sistema. Por eso el servicio
    no tenia como avisarte que se habia quedado sin datos.
    """
    if not (RESEND_API_KEY and EMAIL_TO):
        print("[notify] Correo no configurado (falta RESEND_API_KEY o EMAIL_TO).")
        return False
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                     "Content-Type": "application/json"},
            json={"from": RESEND_FROM, "to": [EMAIL_TO],
                  "subject": subject, "text": body},
            timeout=10,
        )
        if resp.status_code >= 400:
            print(f"[notify] Resend rechazo el correo ({resp.status_code}): {resp.text}")
            return False
        print(f"[notify] Correo enviado: {subject}")
        return True
    except Exception as e:
        print(f"[notify] Error enviando correo via Resend: {e}")
        return False


# ---- Push web (VAPID) ----
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY")
VAPID_CLAIMS_EMAIL = os.environ.get("VAPID_CLAIMS_EMAIL", "mailto:tu_correo@gmail.com")
SUBSCRIPTIONS_FILE = os.environ.get("SUBSCRIPTIONS_FILE", "push_subscriptions.json")
# URL del servidor (server.py en Render) para leer suscripciones cuando
# este script corre en otra maquina (ej. GitHub Actions) y no tiene
# acceso al archivo local que escribe server.py.
SUBSCRIPTIONS_URL = os.environ.get("SUBSCRIPTIONS_URL")


def _b64url_decode(texto):
    """Decodifica base64url agregando el relleno '=' que falte."""
    dato = texto.encode() if isinstance(texto, str) else texto
    dato = dato.rstrip(b"=")
    return base64.urlsafe_b64decode(dato + b"=" * (-len(dato) % 4))


def _limpiar(valor):
    """
    Deja la clave como deberia venir: sin comillas, sin espacios sobrantes
    y con los "\\n" escritos como texto convertidos en saltos reales.
    """
    v = valor.strip()
    # Comillas que a veces quedan al copiar desde un JSON o un .env
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    # Saltos de linea escritos literalmente (caso tipico al pegar un PEM
    # en el formulario de variables de entorno de Render)
    v = v.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    return v


def _vapid_desde_texto(valor):
    """
    Devuelve un objeto Vapid01 a partir de la clave privada en cualquiera
    de los formatos usuales. Lanza ValueError con un mensaje entendible
    si no se puede interpretar.
    """
    v = _limpiar(valor)

    # Caso 1: PEM completo
    if "-----BEGIN" in v:
        try:
            clave = serialization.load_pem_private_key(v.encode(), password=None)
            return Vapid01(clave)
        except Exception as e:
            raise ValueError(f"parece un PEM pero no se pudo leer ({e})")

    # Para los formatos base64 hay que sacar TODO espacio en blanco interno
    compacto = "".join(v.split())

    try:
        crudo = _b64url_decode(compacto)
    except (binascii.Error, ValueError) as e:
        raise ValueError(
            f"no es base64 valido ({e}). Revisa que al pegarla no se haya "
            f"cortado ni se haya colado un caracter de mas."
        )

    # Caso 2: clave raw de 32 bytes (formato estandar de VAPID)
    if len(crudo) == 32:
        clave = ec.derive_private_key(int.from_bytes(crudo, "big"), ec.SECP256R1())
        return Vapid01(clave)

    # Caso 3: DER (PKCS8 o SEC1) codificado en base64
    try:
        clave = serialization.load_der_private_key(crudo, password=None)
        return Vapid01(clave)
    except Exception:
        pass

    if len(crudo) == 65:
        raise ValueError(
            "eso es la clave PUBLICA (65 bytes). VAPID_PRIVATE_KEY debe ser "
            "la privada, de 43 caracteres."
        )

    raise ValueError(
        f"formato no reconocido: decodifica a {len(crudo)} bytes "
        f"(se esperaban 32 para una clave raw, o un DER valido)."
    )


_vapid_cache = {"obj": None, "error": None, "listo": False}


def _get_vapid():
    """Interpreta la clave una sola vez y reutiliza el resultado."""
    if _vapid_cache["listo"]:
        return _vapid_cache["obj"]

    _vapid_cache["listo"] = True
    if not VAPID_PRIVATE_KEY:
        _vapid_cache["error"] = "VAPID_PRIVATE_KEY no esta definida"
        print("[notify] VAPID_PRIVATE_KEY no esta definida; el push queda desactivado.")
        return None
    try:
        _vapid_cache["obj"] = _vapid_desde_texto(VAPID_PRIVATE_KEY)
        print("[notify] Clave VAPID cargada correctamente.")
    except Exception as e:
        _vapid_cache["error"] = str(e)
        print(f"[notify] CLAVE VAPID INVALIDA: {e}")
    return _vapid_cache["obj"]


def public_key_b64(vapid):
    """Clave publica derivada, en el mismo formato que usa la PWA."""
    datos = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(datos).decode().rstrip("=")


def vapid_diagnostico():
    """
    Radiografia de la configuracion de push, para revisar desde el
    navegador. NO expone el valor de la clave privada.
    """
    bruto = VAPID_PRIVATE_KEY
    info = {
        "definida": bool(bruto),
        "largo_bruto": len(bruto) if bruto else 0,
        "largo_esperado_si_es_raw": 43,
    }
    if bruto:
        limpio = "".join(_limpiar(bruto).split())
        info.update({
            "largo_ya_limpia": len(limpio),
            "trae_cabecera_pem": "-----BEGIN" in bruto,
            "trae_saltos_escritos_como_texto": "\\n" in bruto,
            "trae_saltos_reales": "\n" in bruto,
            "trae_comillas": bruto.strip()[:1] in ("\"", "'"),
            "trae_espacios": any(c.isspace() for c in bruto.strip()),
            "empieza_con": bruto.strip()[:4],
            "termina_con": bruto.strip()[-4:],
        })

    vapid = _get_vapid()
    info["clave_valida"] = vapid is not None
    info["error"] = _vapid_cache["error"]
    if vapid is not None:
        info["publica_derivada"] = public_key_b64(vapid)
        info["nota"] = (
            "publica_derivada TIENE que ser identica al VAPID_PUBLIC_KEY "
            "que esta escrito en el HTML de la PWA. Si no coincide, el push "
            "sale firmado con otra identidad y el servicio lo rechaza."
        )
    info["suscripciones_guardadas"] = len(_load_subscriptions())
    info["claims_sub"] = VAPID_CLAIMS_EMAIL
    return info


def _load_subscriptions():
    if SUBSCRIPTIONS_URL:
        try:
            resp = requests.get(SUBSCRIPTIONS_URL, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[notify] Error obteniendo suscripciones desde {SUBSCRIPTIONS_URL}: {e}")
            return []
    if os.path.exists(SUBSCRIPTIONS_FILE):
        try:
            with open(SUBSCRIPTIONS_FILE) as f:
                return json.load(f)
        except Exception as e:
            print(f"[notify] Archivo de suscripciones ilegible: {e}")
            return []
    return []


def _save_subscriptions(subs):
    if SUBSCRIPTIONS_URL:
        return  # en ese modo el archivo lo maneja el otro servidor
    try:
        with open(SUBSCRIPTIONS_FILE, "w") as f:
            json.dump(subs, f, indent=2)
    except Exception as e:
        print(f"[notify] No se pudo guardar suscripciones: {e}")


def enviar_push(payload_dict):
    """
    Manda un push a todos los dispositivos suscritos.
    Devuelve (enviados, fallidos, detalle).

    Las suscripciones que el servicio de notificaciones responde con
    404/410 (la PWA se desinstalo, o el navegador rompio la suscripcion)
    se eliminan, para no reintentarlas cada 10 minutos para siempre.
    """
    vapid = _get_vapid()
    if vapid is None:
        motivo = _vapid_cache["error"] or "clave VAPID no disponible"
        print(f"[notify] Push omitido: {motivo}")
        return 0, 0, [motivo]

    subs = _load_subscriptions()
    if not subs:
        print("[notify] No hay dispositivos suscritos a push todavia.")
        return 0, 0, ["sin suscripciones"]

    payload = json.dumps(payload_dict)
    enviados, fallidos, detalle, vivas = 0, 0, [], []

    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=vapid,
                # Diccionario NUEVO en cada envio: pywebpush le escribe
                # dentro el "aud" y el "exp". Si se reutilizara el mismo
                # objeto, el segundo dispositivo recibiria el "aud" del
                # primero y su servicio de push devolveria 401.
                vapid_claims={"sub": VAPID_CLAIMS_EMAIL},
                ttl=3600,
            )
            enviados += 1
            vivas.append(sub)
        except WebPushException as e:
            codigo = getattr(getattr(e, "response", None), "status_code", None)
            cuerpo = ""
            try:
                cuerpo = e.response.text or ""
            except Exception:
                pass
            fallidos += 1
            # 404/410: la suscripcion ya no existe (PWA desinstalada, etc.)
            # VapidPkHashMismatch, o un 403 que dice "do not correspond to
            # the credentials": la suscripcion se creo con una clave publica
            # VAPID distinta a la actual (por ejemplo, quedo de antes de
            # rotar la clave). En todos estos casos ya no sirve -- antes
            # solo se reconocia el texto exacto "VapidPkHashMismatch", pero
            # algunos servicios (ej. el push de Chrome/Resend) devuelven un
            # 403 con otro texto para el mismo problema, y esa suscripcion
            # se quedaba fallando para siempre sin limpiarse nunca.
            texto_desajuste = "do not correspond to the credentials" in cuerpo
            vencida = (codigo in (404, 410)
                       or "VapidPkHashMismatch" in cuerpo or texto_desajuste)
            if vencida:
                motivo = (f"{codigo}" if codigo in (404, 410)
                          else "VapidPkHashMismatch/clave desajustada")
                detalle.append(f"suscripcion vencida ({motivo}), eliminada")
                print(f"[notify] Suscripcion vencida ({motivo}); se elimina.")
            else:
                vivas.append(sub)
                detalle.append(f"error {codigo}: {e}")
                print(f"[notify] Error enviando push ({codigo}): {e}")
        except Exception as e:
            vivas.append(sub)
            fallidos += 1
            detalle.append(f"{type(e).__name__}: {e}")
            print(f"[notify] Error inesperado enviando push: {type(e).__name__}: {e}")

    if len(vivas) != len(subs):
        _save_subscriptions(vivas)

    return enviados, fallidos, detalle


def send_push_alert(ticker, direccion, precio, avg, indicadores_texto=None):
    """
    Push BIDIRECCIONAL (ver send_alert). Incluye una 'url' en el payload:
    sw.js la usa en notificationclick para abrir la app directo en la
    tarjeta de esa accion, en vez de solo enfocar la pantalla principal.
    """
    if direccion == "venta":
        emoji, palabra, prep = "📈", "posible venta", "sobre"
    else:
        emoji, palabra, prep = "📉", "posible compra", "bajo"

    title = f"{emoji} {ticker} · {palabra}"
    body = f"{precio:,.0f}"
    if avg:
        dist = abs((precio / avg - 1) * 100)
        body += f" ({dist:.1f}% {prep} su promedio de {avg:,.0f})"
    if indicadores_texto:
        body += f" · {indicadores_texto}"

    url = f"{FRONTEND_URL}?ticker={ticker}"
    enviados, fallidos, _ = enviar_push(
        {"title": title, "body": body, "ticker": ticker, "url": url}
    )
    if enviados:
        print(f"[notify] Push enviado para {ticker} a {enviados} dispositivo(s).")


def _fmt_monto(valor, mercado):
    """CLP sin decimales (igual que el resto de la app), USD con 2 decimales."""
    if mercado == "USD":
        return f"US$ {valor:,.2f}"
    return f"CL$ {valor:,.0f}"


def _texto_objetivo(ticker, direccion, precio, objetivo, monto, pct, mercado):
    """
    Arma emoji/título/cuerpo para un cruce de precio objetivo (modulo 'Mi
    Cartera' del frontend, NO las señales de signals.py). A diferencia de
    send_alert()/send_push_alert() -- que comparan contra el promedio de 90
    dias -- aca el usuario puso el numero a mano ("avisar si sube/baja a"),
    asi que el texto siempre muestra el monto Y el porcentaje de distancia
    respecto de ese objetivo, tal como se pidio.
    """
    if direccion == "sube":
        emoji, palabra, verbo = "🎯", "alcanzó tu precio de venta", "subió a"
    else:
        emoji, palabra, verbo = "🔔", "alcanzó tu precio de aviso", "bajó a"

    pct_txt = f"{'+' if pct is not None and pct >= 0 else ''}{pct:.1f}%" if pct is not None else "s/d"
    titulo = f"{emoji} {ticker} {palabra}"
    cuerpo = (
        f"{ticker} {verbo} {_fmt_monto(precio, mercado)}. "
        f"Tu objetivo era {_fmt_monto(objetivo, mercado)} "
        f"({pct_txt} respecto del objetivo, {_fmt_monto(monto, mercado)} de diferencia)."
    )
    return titulo, cuerpo


def send_price_target_alert(ticker, direccion, precio, objetivo, monto, pct, mercado):
    """
    Correo para un precio objetivo definido por el usuario en 'Mi Cartera'
    (imagen 2 del pedido): 'sube' = precio de venta, 'baja' = aviso de
    caída. Cubre Chile Y EE.UU. -- a diferencia de send_alert(), que hoy
    solo corre sobre TICKERS (Chile) porque signals.py no tiene reglas para
    EE.UU. todavia. Siempre incluye el monto Y el porcentaje de distancia.
    """
    if not (RESEND_API_KEY and EMAIL_TO):
        print("[notify] Correo no configurado (falta RESEND_API_KEY o EMAIL_TO), se omite envio.")
        return
    titulo, cuerpo = _texto_objetivo(ticker, direccion, precio, objetivo, monto, pct, mercado)
    body = cuerpo + f"\n\n👉 Toca aquí para ver el detalle en la app:\n{FRONTEND_URL}?ticker={ticker}"
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"from": RESEND_FROM, "to": [EMAIL_TO], "subject": titulo, "text": body},
            timeout=10,
        )
        if resp.status_code >= 400:
            print(f"[notify] Resend rechazo el correo de objetivo ({resp.status_code}): {resp.text}")
        else:
            print(f"[notify] Correo de objetivo enviado para {ticker} (via Resend)")
    except Exception as e:
        print(f"[notify] Error enviando correo de objetivo via Resend: {e}")


def send_price_target_push(ticker, direccion, precio, objetivo, monto, pct, mercado):
    """Push para el mismo evento que send_price_target_alert(), ver esa función."""
    titulo, cuerpo = _texto_objetivo(ticker, direccion, precio, objetivo, monto, pct, mercado)
    url = f"{FRONTEND_URL}?ticker={ticker}"
    enviados, fallidos, _ = enviar_push(
        {"title": titulo, "body": cuerpo, "ticker": ticker, "url": url}
    )
    if enviados:
        print(f"[notify] Push de objetivo enviado para {ticker} a {enviados} dispositivo(s).")
