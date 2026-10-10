"""One-time, owner-only Picker setup on loopback. Never deploy this server."""

import json
import os
import secrets
import time
import webbrowser
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import quote

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials

from app.drive_connection import DRIVE_SCOPES, store_credentials

ROOT = Path(__file__).resolve().parent
ORIGIN = "http://127.0.0.1:8766"
FOLDER_MIME = "application/vnd.google-apps.folder"


def validate_folder(folder, expected_id):
    if folder.get("id") != expected_id or folder.get("mimeType") != FOLDER_MIME:
        raise ValueError("Selecciona la carpeta de la boda que compartiste, no otra carpeta.")
    if folder.get("trashed") or not folder.get("capabilities", {}).get("canAddChildren"):
        raise ValueError("La carpeta no permite agregar archivos con esta cuenta.")


class PickerSetup:
    def __init__(self, credentials, key, api_key, folder_id, root=ROOT):
        self.credentials = credentials
        self.key = key
        self.api_key = api_key
        self.folder_id = folder_id
        self.root = root
        self.bootstrap = secrets.token_urlsafe(32)
        self.session = secrets.token_urlsafe(32)
        self.deadline = time.monotonic() + 900
        self.completed = False

    def config(self):
        if not self.credentials.valid:
            self.credentials.refresh(Request())
            store_credentials(self.credentials.to_json(), self.key, self.root / ".secrets/drive-credentials.enc")
        project_number = self.credentials.client_id.split("-", 1)[0]
        if not project_number.isdigit():
            raise ValueError("El cliente OAuth no contiene un número de proyecto válido.")
        return {"token": self.credentials.token, "apiKey": self.api_key, "appId": project_number}

    def select(self, folder_id):
        if folder_id != self.folder_id:
            raise ValueError("Selecciona la carpeta de la boda que compartiste, no otra carpeta.")
        with AuthorizedSession(self.credentials) as session:
            response = session.get(
                "https://www.googleapis.com/drive/v3/files/" + quote(folder_id, safe=""),
                params={"fields": "id,name,mimeType,trashed,capabilities(canAddChildren)"}, timeout=20,
            )
        if response.status_code != 200:
            raise ValueError("Drive aún no permite acceder a la carpeta. Reabre el selector y selecciónala otra vez.")
        folder = response.json()
        validate_folder(folder, self.folder_id)
        record = {"id": self.folder_id, "verifiedAt": datetime.now(timezone.utc).isoformat()}
        target = self.root / ".secrets/drive-folder.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record), encoding="utf-8")
        store_credentials(self.credentials.to_json(), self.key, self.root / ".secrets/drive-credentials.enc")
        self.completed = True
        return {"message": "Carpeta autorizada y verificada. Puedes cerrar esta pestaña y volver a Codex."}


def make_handler(setup):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # URLs, cookies and OAuth data must never enter shared logs.

        def reply(self, status, body=b"", content_type="text/plain; charset=utf-8", extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def allowed_host(self):
            return self.headers.get("Host") == "127.0.0.1:8766" and time.monotonic() < setup.deadline

        def authenticated(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                value = cookie["picker_setup"].value if "picker_setup" in cookie else ""
            except Exception:
                return False
            return self.allowed_host() and secrets.compare_digest(value, setup.session)

        def json_reply(self, status, data):
            self.reply(status, json.dumps(data).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            if not self.allowed_host():
                return self.reply(403)
            if secrets.compare_digest(self.path, "/setup/" + setup.bootstrap):
                setup.bootstrap = secrets.token_urlsafe(32)  # one-time browser bootstrap
                return self.reply(303, extra={"Location": "/", "Set-Cookie":
                    f"picker_setup={setup.session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=900"})
            if not self.authenticated():
                return self.reply(403, "Abre el selector desde el script local.".encode())
            if self.path == "/":
                return self.reply(200, (ROOT / "setup/picker.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/config":
                try:
                    return self.json_reply(200, setup.config())
                except Exception:
                    return self.json_reply(503, {"message": "No se pudo renovar la conexión con Google. Vuelve a conectar tu cuenta."})
            return self.reply(404)

        def do_POST(self):
            # Drain small bodies before returning 403; otherwise Windows may reset
            # the connection before the browser receives the denial response.
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.reply(400)
            body = self.rfile.read(size) if 0 < size <= 2048 else b""
            if not self.authenticated() or self.headers.get("Origin") != ORIGIN:
                return self.reply(403)
            if self.path != "/select":
                return self.reply(404)
            try:
                if size <= 0 or size > 2048:
                    return self.reply(400)
                payload = json.loads(body)
                if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
                    return self.reply(400)
                result = setup.select(payload["id"])
                return self.json_reply(200, result)
            except ValueError as error:
                # Only controlled local validation messages, never Google response text.
                message = str(error) if type(error) is ValueError and not isinstance(error, json.JSONDecodeError) else "Selección inválida."
                return self.json_reply(400, {"message": message})
            except Exception:
                return self.json_reply(503, {"message": "No se pudo verificar Drive. Intenta otra vez."})

    return Handler


def main():
    load_dotenv(ROOT / ".env")
    key = os.environ["DRIVE_TOKEN_ENCRYPTION_KEY"]
    serialized = Fernet(key.encode()).decrypt((ROOT / ".secrets/drive-credentials.enc").read_bytes())
    data = json.loads(serialized)
    if set(data.get("scopes", [])) != set(DRIVE_SCOPES):
        raise ValueError("Solo se admite drive.file.")
    setup = PickerSetup(Credentials.from_authorized_user_info(data), key,
                        os.environ["GOOGLE_PICKER_API_KEY"], os.environ["GOOGLE_DRIVE_FOLDER_ID"])
    with HTTPServer(("127.0.0.1", 8766), make_handler(setup)) as server:
        server.timeout = 1
        webbrowser.open(ORIGIN + "/setup/" + setup.bootstrap)
        print("Selector abierto en el navegador. Elige la carpeta de la boda. Espera máxima: 15 minutos.", flush=True)
        while not setup.completed and time.monotonic() < setup.deadline:
            server.handle_request()
    if not setup.completed:
        print("Terminó la espera sin seleccionar una carpeta. Ejecuta el selector nuevamente.", flush=True)
        return 1
    print("Carpeta autorizada y verificada con permiso para agregar archivos. Subidas públicas todavía desactivadas.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Selector cancelado.")
        raise SystemExit(1)
    except Exception as error:
        print(f"No se pudo iniciar el selector ({type(error).__name__}). Revisa la configuración local.")
        raise SystemExit(1)
