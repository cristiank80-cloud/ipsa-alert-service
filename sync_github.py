"""
Sincronización con GitHub para estado.json cifrado.

El navegador envía estado CIFRADO (el backend no puede leerlo).
Este módulo solo actúa como proxy: recibe bytes cifrados, los sube a GitHub,
y descarga bytes cifrados de GitHub para reenviárselos al navegador.
"""
import os
import json
import base64
import urllib.request
import urllib.error
from datetime import datetime

GITHUB_OWNER = os.environ.get("SYNC_GITHUB_OWNER", "")
GITHUB_REPO = os.environ.get("SYNC_GITHUB_REPO", "")
GITHUB_TOKEN = os.environ.get("SYNC_GITHUB_TOKEN", "")
GITHUB_API = "https://api.github.com"

STATE_FILENAME = "estado.json"


def _hacer_request(url, method="GET", data=None, token=None):
    """Hace un request a GitHub API con autenticación."""
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "ipsa-sync",
    }

    if token:
        headers["Authorization"] = f"token {token}"

    req = urllib.request.Request(url, headers=headers, method=method)

    if data:
        if isinstance(data, dict):
            data = json.dumps(data).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        req.data = data
        headers["Content-Type"] = "application/json"

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8")
            return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        return e.code, body


def _obtener_sha(token):
    """Obtiene el SHA del archivo actual en GitHub."""
    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{STATE_FILENAME}"
    try:
        status, body = _hacer_request(url, method="GET", token=token)
        if status == 200:
            data = json.loads(body)
            return data.get("sha")
        elif status == 404:
            return None
        else:
            raise Exception(f"GitHub error {status}: {body}")
    except Exception as e:
        raise Exception(f"No se pudo obtener SHA: {str(e)}")


def descargar_estado(token=None):
    """Descarga estado.json cifrado de GitHub."""
    if not GITHUB_OWNER or not GITHUB_REPO:
        raise Exception("Falta SYNC_GITHUB_OWNER o SYNC_GITHUB_REPO")

    token = token or GITHUB_TOKEN
    if not token:
        raise Exception("Falta SYNC_GITHUB_TOKEN")

    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{STATE_FILENAME}"

    try:
        status, body = _hacer_request(url, method="GET", token=token)

        if status == 200:
            data = json.loads(body)
            return {
                "content": data.get("content"),
                "sha": data.get("sha"),
            }
        elif status == 404:
            return None
        else:
            raise Exception(f"GitHub error {status}: {body}")
    except Exception as e:
        raise Exception(f"Descarga falló: {str(e)}")


def subir_estado(contenido_b64, mensaje_commit=None, token=None):
    """Sube estado.json cifrado a GitHub."""
    if not GITHUB_OWNER or not GITHUB_REPO:
        raise Exception("Falta SYNC_GITHUB_OWNER o SYNC_GITHUB_REPO")

    token = token or GITHUB_TOKEN
    if not token:
        raise Exception("Falta SYNC_GITHUB_TOKEN")

    sha_actual = _obtener_sha(token)

    if not mensaje_commit:
        ahora = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        mensaje_commit = f"Sincronización automática - {ahora}"

    payload = {
        "message": mensaje_commit,
        "content": contenido_b64,
    }

    if sha_actual:
        payload["sha"] = sha_actual

    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{STATE_FILENAME}"

    try:
        status, body = _hacer_request(url, method="PUT", data=payload, token=token)

        if status in (201, 200):
            data = json.loads(body)
            nuevo_sha = data.get("content", {}).get("sha")

            return {
                "sha": nuevo_sha,
                "message": "Creado" if status == 201 else "Actualizado",
            }
        else:
            raise Exception(f"GitHub error {status}: {body}")
    except Exception as e:
        raise Exception(f"Subida falló: {str(e)}")


def verificar_credenciales():
    """Verifica que las credenciales de GitHub sean válidas."""
    if not GITHUB_OWNER or not GITHUB_REPO or not GITHUB_TOKEN:
        raise Exception("Falta configurar credenciales de GitHub en Render")

    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
    try:
        status, body = _hacer_request(url, method="GET", token=GITHUB_TOKEN)
        if status == 200:
            return True
        elif status == 401:
            raise Exception("Token de GitHub inválido")
        elif status == 404:
            raise Exception(f"Repositorio {GITHUB_OWNER}/{GITHUB_REPO} no encontrado")
        else:
            raise Exception(f"Error verificando credenciales: {status}")
    except Exception as e:
        raise Exception(f"No se pudo verificar credenciales: {str(e)}")
