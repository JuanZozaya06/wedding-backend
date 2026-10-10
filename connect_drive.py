"""Run manually on the owner's computer; never run as a public web endpoint."""

import argparse
import os
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv, set_key
from google_auth_oauthlib.flow import InstalledAppFlow

from app.drive_connection import DRIVE_SCOPES, store_credentials, validate_client_file

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Conectar la cuenta propietaria de Drive localmente.")
    parser.add_argument("--client-secrets", type=Path, required=True, help="Ruta al JSON OAuth descargado de Google.")
    args = parser.parse_args()
    validate_client_file(args.client_secrets)
    load_dotenv(ROOT / ".env")
    key = os.getenv("DRIVE_TOKEN_ENCRYPTION_KEY")
    if not key:
        key = Fernet.generate_key().decode("ascii")
        set_key(str(ROOT / ".env"), "DRIVE_TOKEN_ENCRYPTION_KEY", key)
    # Fail before launching consent if the local encryption configuration is invalid.
    Fernet(key.encode("ascii"))
    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secrets), scopes=DRIVE_SCOPES)
    credentials = flow.run_local_server(
        host="localhost", bind_addr="127.0.0.1", port=8765,
        authorization_prompt_message="Se abrirá Google para que autorices tu cuenta. No compartas la URL de autorización.",
        success_message="Autorización recibida. Puedes cerrar esta pestaña y volver a Codex.",
        prompt="consent", timeout_seconds=900,
    )
    store_credentials(credentials.to_json(), key, ROOT / ".secrets" / "drive-credentials.enc")
    print("Conexión guardada cifrada. Falta autorizar la carpeta con Picker; las subidas siguen desactivadas.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Conexión cancelada.")
        raise SystemExit(1)
    except Exception as error:
        # OAuth exceptions may contain codes or tokens. Do not echo them to shared logs.
        if type(error).__name__ == "WSGITimeoutError":
            print("Terminó el tiempo de espera del callback local. Inicia una autorización nueva.")
        else:
            print(f"No se completó la conexión ({type(error).__name__}). Revisa el JSON, el callback local y el usuario de prueba de Google.")
        raise SystemExit(1)
