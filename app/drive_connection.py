"""Local setup of a single Drive owner. No public authorization endpoints."""

import json
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
LOCAL_REDIRECT_URI = "http://localhost:8765/"


def validate_client_file(path: Path) -> None:
    config = json.loads(path.read_text(encoding="utf-8"))
    client = config.get("web", {})
    if not client.get("client_id") or not client.get("client_secret"):
        raise ValueError("Descarga el JSON de un cliente OAuth de tipo Aplicación web.")
    if LOCAL_REDIRECT_URI not in client.get("redirect_uris", []):
        raise ValueError(f"Agrega {LOCAL_REDIRECT_URI} a los URI de redirección del cliente.")


def store_credentials(serialized: str, key: str, target: Path) -> None:
    """Validate before replacing an existing connection and write atomically."""
    data = json.loads(serialized)
    if not data.get("refresh_token"):
        raise ValueError("Google no entregó autorización persistente. Vuelve a conectar con consentimiento.")
    scopes = set(data.get("scopes", []))
    if not set(DRIVE_SCOPES).issubset(scopes) or scopes - set(DRIVE_SCOPES):
        raise ValueError("La conexión debe incluir únicamente el permiso drive.file.")
    encrypted = Fernet(key.encode("ascii")).encrypt(serialized.encode("utf-8"))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encrypted)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
