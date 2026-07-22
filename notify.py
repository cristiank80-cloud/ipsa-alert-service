"""
Envio de alertas por dos canales:
  - Correo, via SMTP (funciona con Gmail usando una "contraseña de
    aplicacion", no tu clave normal).
  - Push al celular, via Web Push (VAPID) hacia la PWA instalada.
"""
import os
import json
import smtplib
from email.mime.text import MIMEText

from pywebpush import webpush, WebPushException

# ---- Correo ----
EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))


def send_email_alert(ticker, price, avg, pct_below, indicadores_texto=None):
    if not (EMAIL_FROM and EMAIL_APP_PASSWORD and EMAIL_TO):
        print("[notify] Correo no configurado, se omite envío.")
        return

    subject = f"⚠️ {ticker} cayó {pct_below:.1f}% bajo su promedio"
    body = (
        f"{ticker} está en {price:,.0f}, un {pct_below:.1f}% bajo su promedio "
        f"histórico de 90 días ({avg:,.0f}).\n\n"
    )
    if indicadores_texto:
        body += f"Indicadores técnicos: {indicadores_texto}\n\n"
    body += "Revisa la app para más detalle antes de decidir."
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        print(f"[notify] Correo enviado para {ticker}")
    except Exception as e:
        print(f"[notify] Error enviando correo: {e}")


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
