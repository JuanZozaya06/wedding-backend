"""Drive access uses the owner's drive.file credentials, never the Firebase service account."""
import hashlib
import io
import json
import os
import threading

import httplib2
from cryptography.fernet import Fernet
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from fastapi import HTTPException

from app.drive_connection import DRIVE_SCOPES, store_credentials


class DriveStore:
    def __init__(self, root, key, folder_id):
        self.root, self.key, self.folder_id = root, key, folder_id
        self.lock = threading.Lock()
        encrypted = os.getenv("DRIVE_CREDENTIALS_ENCRYPTED", "").strip()
        self.local_credentials = not bool(encrypted)
        if self.local_credentials:
            marker = json.loads((root / ".secrets/drive-folder.json").read_text())
            if marker["id"] != folder_id:
                raise ValueError("Selecciona la carpeta con Picker antes de habilitar las subidas.")
            ciphertext = (root / ".secrets/drive-credentials.enc").read_bytes()
        else:
            ciphertext = encrypted.encode("ascii")
        info = json.loads(Fernet(key.encode("ascii")).decrypt(ciphertext))
        if set(info.get("scopes", [])) != set(DRIVE_SCOPES):
            raise ValueError("Solo se admite drive.file.")
        if not info.get("refresh_token"):
            raise ValueError("Falta el permiso persistente de Drive.")
        self.credentials = Credentials.from_authorized_user_info(info)
        configured_thumbnails = os.getenv("GOOGLE_DRIVE_THUMBNAILS_FOLDER_ID", "").strip()
        if not self.local_credentials and not configured_thumbnails:
            raise ValueError("Configura la carpeta de miniaturas existente antes de desplegar.")
        with self.client() as drive:
            folder = drive.files().get(fileId=folder_id, fields="mimeType,trashed,capabilities(canAddChildren)").execute()
            if folder.get("trashed") or folder.get("mimeType") != "application/vnd.google-apps.folder" or not folder.get("capabilities", {}).get("canAddChildren"):
                raise ValueError("Drive no permite agregar archivos a la carpeta.")
            # A single local bootstrap; reuse on later starts. No sharing changes.
            reference = root / ".secrets/drive-thumbnails.json"
            if configured_thumbnails or reference.exists():
                self.thumbnails = configured_thumbnails or json.loads(reference.read_text())["id"]
                thumbnail_folder = drive.files().get(fileId=self.thumbnails, fields="mimeType,parents,trashed").execute()
                if thumbnail_folder.get("trashed") or folder_id not in thumbnail_folder.get("parents", []) or thumbnail_folder.get("mimeType") != "application/vnd.google-apps.folder":
                    raise ValueError("La carpeta de miniaturas ya no está disponible.")
            else:
                self.thumbnails = drive.files().create(body={"name": "Miniaturas del álbum", "mimeType": "application/vnd.google-apps.folder", "parents": [folder_id]}, fields="id").execute()["id"]
                reference.write_text(json.dumps({"id": self.thumbnails}), encoding="utf-8")

    def client(self):
        with self.lock:
            if not self.credentials.valid:
                self.credentials.refresh(Request())
                if self.local_credentials:
                    store_credentials(self.credentials.to_json(), self.key, self.root / ".secrets/drive-credentials.enc")
        http = httplib2.Http(timeout=30)
        # Drive uses 308 for an incomplete upload, not a redirect. Custom
        # transports must mirror googleapiclient.http.build_http's setting.
        http.redirect_codes = http.redirect_codes - {308}
        return build("drive", "v3", http=AuthorizedHttp(self.credentials, http=http), cache_discovery=False)

    def generate_ids(self):
        with self.client() as drive:
            return drive.files().generateIds(count=2, space="drive").execute()["ids"]

    def put(self, file_id, data, filename, mime, thumbnail=False):
        with self.client() as drive:
            try:
                existing = drive.files().get(fileId=file_id, fields="id,size,md5Checksum,trashed").execute()
                if existing.get("trashed") or existing.get("md5Checksum") != hashlib.md5(data).hexdigest():
                    raise HTTPException(409, "El archivo de este intento cambió. Elige una nueva tanda.")
                return  # A retry after a lost response must not duplicate files.
            except HttpError as error:
                if error.resp.status != 404:
                    raise
            quota = drive.about().get(fields="storageQuota").execute().get("storageQuota", {})
            if quota.get("limit") and int(quota["limit"]) - int(quota.get("usage", 0)) < len(data) + 1024 * 1024:
                raise HTTPException(507, "El álbum no tiene espacio disponible. Avísales a los novios.")
            upload = drive.files().create(
                body={"id": file_id, "name": filename, "mimeType": mime, "parents": [self.thumbnails if thumbnail else self.folder_id]},
                media_body=MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, chunksize=1024 * 1024, resumable=True), fields="id",
            )
            result = None
            try:
                while result is None:
                    _, result = upload.next_chunk(num_retries=2)
            except HttpError as error:
                if error.resp.status == 409:
                    # Another attempt may have finished after a timeout. Verify on retry.
                    raise HTTPException(409, "La foto se está verificando. Reintenta en unos segundos.") from None
                raise

    def read(self, file_id):
        with self.client() as drive:
            return drive.files().get_media(fileId=file_id).execute(num_retries=2)
