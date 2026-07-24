"""
Envio de alertas por dos canales:
  - Correo, via la API de Resend (HTTPS, no SMTP -- Render y varios
    hostings gratuitos bloquean el puerto SMTP saliente, por eso se
    migró de smtplib a esto).
  - Push al celular, via Web Push (VAPID) hacia la PWA instalada.
"""
import os
import json
import requests

from pywebpush import webpush, WebPushException

# ---- Correo (API de Resend) ----
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
# El remitente "sandbox" de Resend no requiere verificar un dominio,
# pero SOLO puede mandar al correo con el que te registraste en Resend
# -- que es justo tu caso, te mandas las alertas a ti mismo.
RESEND_FROM = os.environ.get("RESEND_FROM", "onboarding@resend.dev")
EMAIL_TO = os.environ.get("EMAIL_TO")


def send_email_alert(ticker, price, avg, pct_below, indicadores_texto=None):
    if not (RESEND_API_KEY and EMAIL_TO):
        print("[notify] Correo no configurado (falta RESEND_API_KEY o EMAIL_TO), se omite envío.")
        return

    subject = f"⚠️ {ticker} cayó {pct_below:.1f}% bajo su promedio"
    body = (
        f"{ticker} está en {price:,.0f}, un {pct_below:.1f}% bajo su promedio "
        f"histórico de 90 días ({avg:,.0f}).\n\n"
    )
    if indicadores_texto:
        body += f"Indicadores técnicos: {indicadores_texto}\n\n"
    body += "Revisa la app para más detalle antes de decidir."

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
            print(f"[notify] Resend rechazó el correo ({resp.status_code}): {resp.text}")
        else:
            print(f"[notify] Correo enviado para {ticker} (vía Resend)")
    except Exception as e:
        print(f"[notify] Error enviando correo vía Resend: {e}")


# ---- Push web (VAPID) ----
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY")
VAPID_CLAIMS_EMAIL = os.environ.get("VAPID_CLAIMS_EMAIL", "mailto:tu_correo@gmail.com")
SUBSCRIPTIONS_FILE = os.environ.get("SUBSCRIPTIONS_FILE", "push_subscriptions.json")
# URL del servidor (server.py en Render) para leer suscripciones cuando
# este script corre en otra máquina (ej. GitHub Actions) y no tiene
# acceso al archivo local que escribe server.py.
SUBSCRIPTIONS_URL = os.environ.get("SUBSCRIPTIONS_URL")


def _load_subscriptions():
    if SUBSCRIPTIONS_URL:
        try:
            import requests
            resp = requests.get(SUBSCRIPTIONS_URL, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[notify] Error obteniendo suscripciones desde {SUBSCRIPTIONS_URL}: {e}")
            return []
    if os.path.exists(SUBSCRIPTIONS_FILE):
        with open(SUBSCRIPTIONS_FILE) as f:
            return json.load(f)
    return []


def send_push_alert(ticker, price, avg, pct_below, indicadores_texto=None):
    if not VAPID_PRIVATE_KEY:
        print("[notify] Push no configurado (falta VAPID_PRIVATE_KEY), se omite.")
        return

    subs = _load_subscriptions()
    if not subs:
        print("[notify] No hay dispositivos suscritos a push todavía.")
        return

    title = f"{ticker} bajo su promedio"
    body = f"{price:,.0f} ({pct_below:.1f}% bajo el promedio de {avg:,.0f})"
    if indicadores_texto:
        body += f" · {indicadores_texto}"
    payload = json.dumps({"title": title, "body": body, "ticker": ticker})

    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIMS_EMAIL},
            )
            print(f"[notify] Push enviado para {ticker}")
        except WebPushException as e:
            print(f"[notify] Error enviando push (¿suscripción vencida?): {e}")
