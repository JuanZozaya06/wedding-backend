"""Only the Admin SDK accesses these collections; browser rules deny them by default."""
import hashlib
import hmac
import time
import unicodedata
from datetime import datetime, timezone

from fastapi import HTTPException
from firebase_admin import firestore

BATCHES = "wedding_photo_batches"
PHOTOS = "wedding_photos"
SYSTEM = "wedding_photo_system"


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def normalize(value):
    return "".join(c for c in unicodedata.normalize("NFD", value) if unicodedata.category(c) != "Mn").casefold()


def public_photo(record):
    return {key: record[key] for key in ("id", "batchId", "name", "message", "createdAt", "fileName", "mimeType", "size")}


class Catalog:
    def __init__(self, database, max_bytes, max_photos):
        self.db, self.max_bytes, self.max_photos = database, max_bytes, max_photos

    def check_admin(self, key):
        config = self.db.collection("admin").document("config").get(timeout=15).to_dict() or {}
        expected = config.get("masterKeyHash", "").strip()
        return bool(expected) and hmac.compare_digest(hashlib.sha256(key.strip().encode()).hexdigest(), expected)

    def create_batch(self, batch_id, name, message):
        ref = self.db.collection(BATCHES).document(batch_id)
        usage_ref = self.db.collection(SYSTEM).document("usage")
        @firestore.transactional
        def create(transaction):
            usage = usage_ref.get(transaction=transaction).to_dict() or {}
            if usage.get("batches", 0) >= self.max_photos * 3:
                raise HTTPException(507, "El álbum alcanzó su capacidad. Avísales a los novios.")
            transaction.create(ref, {"id": batch_id, "name": name.strip(), "message": message.strip(), "createdAt": timestamp(), "expiresAt": time.time() + 86400, "photoCount": 0})
            transaction.set(usage_ref, {"batches": usage.get("batches", 0) + 1}, merge=True)
        create(self.db.transaction())

    def find(self, photo_id):
        return self.db.collection(PHOTOS).document(photo_id).get(timeout=15).to_dict()

    def claim(self, batch_id, photo_id, filename, mime, size, total_size, checksum, ids):
        batch_ref = self.db.collection(BATCHES).document(batch_id)
        ref = self.db.collection(PHOTOS).document(photo_id)
        usage_ref = self.db.collection(SYSTEM).document("usage")

        @firestore.transactional
        def reserve(transaction):
            batch = batch_ref.get(transaction=transaction).to_dict()
            existing = ref.get(transaction=transaction).to_dict()
            usage = {"bytes": 0, "photos": 0, **(usage_ref.get(transaction=transaction).to_dict() or {})}
            if not batch or batch["expiresAt"] < time.time():
                raise HTTPException(401, "Esta tanda venció. Vuelve a elegir tus fotos.")
            if existing:
                if existing["batchId"] != batch_id or existing["sha256"] != checksum:
                    raise HTTPException(409, "Esta foto cambió desde el primer intento. Elige una nueva tanda.")
                if existing["status"] == "ready":
                    return existing
                if existing.get("leaseUntil", 0) > time.time():
                    raise HTTPException(409, "Esta foto sigue subiendo. Reintenta en unos segundos.")
                transaction.update(ref, {"leaseUntil": time.time() + 600})
                return existing
            if batch["photoCount"] >= 20:
                raise HTTPException(400, "Puedes compartir hasta 20 fotos por tanda.")
            if usage["bytes"] + total_size > self.max_bytes or usage["photos"] >= self.max_photos:
                raise HTTPException(507, "El álbum alcanzó su capacidad. Avísales a los novios.")
            record = {"id": photo_id, "batchId": batch_id, "name": batch["name"], "message": batch["message"], "createdAt": timestamp(), "fileName": filename, "mimeType": mime, "size": size, "reservedBytes": total_size, "sha256": checksum, "driveFileId": ids[0], "thumbnailDriveId": ids[1], "status": "uploading", "leaseUntil": time.time() + 600}
            transaction.create(ref, record)
            transaction.update(batch_ref, {"photoCount": batch["photoCount"] + 1})
            transaction.set(usage_ref, {"bytes": usage["bytes"] + total_size, "photos": usage["photos"] + 1}, merge=True)
            return record
        return reserve(self.db.transaction())

    def finish(self, photo_id):
        self.db.collection(PHOTOS).document(photo_id).update({"status": "ready", "leaseUntil": 0}, timeout=15)

    def retry_later(self, photo_id):
        # Keep the reservation/Drive IDs: retries reuse them even after partial uploads.
        self.db.collection(PHOTOS).document(photo_id).update({"leaseUntil": 0}, timeout=15)

    def list(self, cursor=None, search="", limit=30):
        query = self.db.collection(PHOTOS).order_by("createdAt", direction=firestore.Query.DESCENDING)
        if cursor:
            document = self.db.collection(PHOTOS).document(cursor).get(timeout=15)
            if not document.exists:
                raise HTTPException(400, "Actualiza la galería para continuar.")
            query = query.start_after(document)
        records, last, more = [], None, False
        search = normalize(search.strip())
        # Search the album on the server so matches aren't limited to loaded thumbnails.
        for document in query.limit(self.max_photos + 1).stream(timeout=30):
            record = document.to_dict()
            if len(records) >= limit:
                more = True
                break
            last = document.id
            if record["status"] == "ready" and (not search or search in normalize(f'{record["name"]} {record["message"]} {record["fileName"]}')):
                records.append(public_photo(record))
        return {"photos": records, "nextCursor": last if more else None}
