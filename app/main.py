import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

import firebase_admin
from firebase_admin import credentials, firestore
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.catalog import Catalog, public_photo
from app.drive import DriveStore
from app.images import MAX_BYTES, prepare_image

ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("wedding.photos")


class BatchInput(BaseModel):
    name: str = Field(default="", max_length=100)
    message: str = Field(default="", max_length=1000)


class LoginInput(BaseModel):
    key: str = Field(min_length=1, max_length=300)


class RateLimiter:
    def __init__(self):
        self.events = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, kind, address, limit, window):
        now = time.monotonic()
        with self.lock:
            if len(self.events) >= 4096:
                self.events = defaultdict(deque, {k: v for k, v in self.events.items() if v and v[-1] > now - 3600})
                if len(self.events) >= 4096 and (kind, address) not in self.events:
                    raise HTTPException(429, "Hay muchas solicitudes. Intenta nuevamente en un minuto.")
            queue = self.events[(kind, address)]
            while queue and queue[0] < now - window:
                queue.popleft()
            if len(queue) >= limit:
                raise HTTPException(429, "Espera un momento antes de reintentar.", headers={"Retry-After": "60"})
            queue.append(now)


class Runtime:
    def __init__(self, catalog, drive, signing_key):
        self.catalog, self.drive = catalog, drive
        self.tokens = URLSafeTimedSerializer(signing_key)
        self.upload_slots = threading.BoundedSemaphore(2)

    def issue(self, subject, purpose):
        return self.tokens.dumps({"subject": subject}, salt=purpose)

    def verify(self, token, subject, purpose, max_age):
        try:
            payload = self.tokens.loads(token, salt=purpose, max_age=max_age)
            if not secrets.compare_digest(str(payload.get("subject", "")), subject):
                raise BadSignature("wrong subject")
        except BadSignature:
            raise HTTPException(401, "La sesión venció o no es válida. Vuelve a entrar.") from None


def configured_runtime():
    if os.getenv("PHOTOS_ENABLED", "false").lower() != "true":
        return None
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    certificate_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    certificate = credentials.Certificate(json.loads(certificate_json) if certificate_json else os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    if certificate.project_id != project:
        raise ValueError("Firebase pertenece a otro proyecto.")
    try:
        firebase = firebase_admin.get_app("wedding-photos")
    except ValueError:
        firebase = firebase_admin.initialize_app(certificate, {"projectId": project}, name="wedding-photos")
    database = firestore.client(firebase)
    database.collection("admin").document("config").get(timeout=15)
    drive = DriveStore(ROOT, os.environ["DRIVE_TOKEN_ENCRYPTION_KEY"], os.environ["GOOGLE_DRIVE_FOLDER_ID"])
    catalog = Catalog(database, int(os.getenv("PHOTO_MAX_TOTAL_BYTES", str(5 * 1024**3))), int(os.getenv("PHOTO_MAX_COUNT", "2000")))
    return Runtime(catalog, drive, os.environ["PHOTO_SIGNING_KEY"])


def create_app(runtime=None):
    @asynccontextmanager
    async def lifespan(app):
        if app.state.runtime is None:
            try:
                app.state.runtime = await run_in_threadpool(configured_runtime)
            except Exception as error:
                logger.error("Photo configuration unavailable (%s)", type(error).__name__)
        yield

    app = FastAPI(title="G&J · Fotos de la boda", version="0.2.0", lifespan=lifespan)
    app.state.runtime = runtime
    origins = [s.strip() for s in os.getenv("ALLOWED_ORIGINS", "http://localhost:4200,http://127.0.0.1:4200").split(",") if s.strip()]
    rates = RateLimiter()
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"], expose_headers=["Content-Disposition", "Retry-After"])

    @app.middleware("http")
    async def limits(request, call_next):
        origin = request.headers.get("origin")
        if request.url.path.startswith("/api/") and origin and origin not in origins:
            return JSONResponse({"detail": "Origen no permitido."}, status_code=403)
        if request.url.path.startswith("/api/") and request.method == "POST":
            try:
                size = int(request.headers.get("content-length", "0"))
            except ValueError:
                size = 0
            maximum = MAX_BYTES + 16384 if request.url.path.endswith("/file") else 8192
            if not 0 < size <= maximum:
                return JSONResponse({"detail": "La solicitud supera el tamaño permitido."}, status_code=413)
        return await call_next(request)

    @app.exception_handler(Exception)
    async def unexpected(request, error):
        logger.error("Photo operation failed (%s)", type(error).__name__)
        return JSONResponse({"detail": "No pudimos completar la operación. Tus fotos guardadas siguen disponibles; reintenta en un momento."}, status_code=503)

    def active(request: Request):
        current = request.app.state.runtime
        if current is None:
            raise HTTPException(503, "La recepción de fotos aún no está disponible.")
        return current

    def bearer(request):
        value = request.headers.get("authorization", "")
        if not value.startswith("Bearer "):
            raise HTTPException(401, "Vuelve a entrar para continuar.")
        return value[7:]

    def admin(request: Request, current=Depends(active)):
        current.verify(bearer(request), "owner", "admin", 7200)
        return current

    @app.get("/health")
    def health():
        return {"status": "ok", "uploads_enabled": app.state.runtime is not None}

    @app.get("/ready")
    def ready():
        if app.state.runtime is None:
            raise HTTPException(503, "La recepción de fotos aún no está disponible.")
        return {"status": "ready"}

    @app.post("/api/admin/session")
    def login(payload: LoginInput, request: Request, current=Depends(active)):
        address = request.client.host if request.client else "unknown"
        rates.check("login", address, 5, 300)
        rates.check("login-global", "all", 100, 300)
        if not current.catalog.check_admin(payload.key):
            raise HTTPException(401, "La clave del admin no coincide.")
        return {"token": current.issue("owner", "admin"), "expiresIn": 7200}

    @app.post("/api/photo-batches", status_code=201)
    def create_batch(payload: BatchInput, request: Request, current=Depends(active)):
        rates.check("batch", request.client.host if request.client else "unknown", 300, 3600)
        batch_id = str(uuid4())
        current.catalog.create_batch(batch_id, payload.name, payload.message)
        return {"id": batch_id, "token": current.issue(batch_id, "batch")}

    @app.post("/api/photo-batches/{batch_id}/photos/{photo_id}/file")
    def upload(batch_id: UUID, photo_id: UUID, request: Request, file: UploadFile = File(...), current=Depends(active)):
        batch_id, photo_id = str(batch_id), str(photo_id)
        current.verify(bearer(request), batch_id, "batch", 86400)
        rates.check("upload", request.client.host if request.client else "unknown", 100, 60)
        if not current.upload_slots.acquire(blocking=False):
            raise HTTPException(429, "Hay fotos subiendo. Reintenta en unos segundos.", headers={"Retry-After": "5"})
        claimed = False
        try:
            data = file.file.read(MAX_BYTES + 1)
            filename, mime, thumbnail = prepare_image(data, file.filename or "foto")
            checksum = hashlib.sha256(data).hexdigest()
            existing = current.catalog.find(photo_id)
            ids = [existing["driveFileId"], existing["thumbnailDriveId"]] if existing else current.drive.generate_ids()
            record = current.catalog.claim(batch_id, photo_id, filename, mime, len(data), len(data) + len(thumbnail), checksum, ids)
            if record["status"] == "ready":
                return public_photo(record)
            claimed = True
            current.drive.put(record["driveFileId"], data, filename, mime)
            current.drive.put(record["thumbnailDriveId"], thumbnail, photo_id + ".jpg", "image/jpeg", thumbnail=True)
            current.catalog.finish(photo_id)
            claimed = False
            return public_photo(record)
        finally:
            if claimed:
                try:
                    current.catalog.retry_later(photo_id)
                except Exception:
                    pass
            file.file.close()
            current.upload_slots.release()

    @app.get("/api/admin/photos")
    def list_photos(cursor: UUID | None = None, search: str = "", current=Depends(admin)):
        if len(search) > 200:
            raise HTTPException(400, "La búsqueda es demasiado larga.")
        return current.catalog.list(str(cursor) if cursor else None, search)

    @app.get("/api/admin/photos/{photo_id}/{variant}")
    def photo_file(photo_id: UUID, variant: str, current=Depends(admin)):
        if variant not in ("thumbnail", "original"):
            raise HTTPException(404)
        record = current.catalog.find(str(photo_id))
        if not record or record["status"] != "ready":
            raise HTTPException(404, "No encontramos esta foto en el álbum.")
        original = variant == "original"
        data = current.drive.read(record["driveFileId"] if original else record["thumbnailDriveId"])
        return Response(data, media_type=record["mimeType"] if original else "image/jpeg", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Content-Disposition": f"{'attachment' if original else 'inline'}; filename*=UTF-8''{quote(record['fileName'] if original else 'miniatura.jpg', safe='')}"})

    return app


app = create_app()
