import hashlib
import io
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from app.catalog import Catalog, normalize, public_photo
from app.images import MAX_BYTES, prepare_image
from app.main import RateLimiter, Runtime, create_app


def image_bytes(kind="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "#a285b0").save(output, kind)
    return output.getvalue()


class MemoryCatalog:
    def __init__(self):
        self.batches, self.photos = {}, {}

    def check_admin(self, key):
        return key == "owner-password"

    def create_batch(self, id, name, message):
        self.batches[id] = {"name": name, "message": message}

    def find(self, id):
        return self.photos.get(id)

    def claim(self, batch_id, photo_id, filename, mime, size, total, sha, ids):
        if photo_id in self.photos:
            record = self.photos[photo_id]
            if record["batchId"] != batch_id or record["sha256"] != sha:
                raise HTTPException(409)
            return record
        record = {"id": photo_id, "batchId": batch_id, **self.batches[batch_id], "fileName": filename, "mimeType": mime, "size": size, "createdAt": "2026-10-10T03:00:00Z", "sha256": sha, "driveFileId": ids[0], "thumbnailDriveId": ids[1], "status": "uploading"}
        self.photos[photo_id] = record
        return record

    def finish(self, id):
        self.photos[id]["status"] = "ready"

    def retry_later(self, id):
        pass

    def list(self, cursor, search):
        return {"photos": [public_photo(p) for p in self.photos.values() if p["status"] == "ready"], "nextCursor": None}


class PhotoApiTests(unittest.TestCase):
    def setUp(self):
        self.catalog = MemoryCatalog()
        self.drive = Mock()
        self.drive.generate_ids.return_value = ["original-id", "thumbnail-id"]
        self.drive.read.return_value = image_bytes()
        self.runtime = Runtime(self.catalog, self.drive, "test-only-signing-key")
        self.client = TestClient(create_app(self.runtime), raise_server_exceptions=False)

    def batch(self):
        response = self.client.post('/api/photo-batches', json={"name": "María", "message": "¡Qué alegría!"})
        self.assertEqual(response.status_code, 201)
        return response.json()

    def send(self, batch, id=None, data=None):
        return self.client.post(f'/api/photo-batches/{batch["id"]}/photos/{id or uuid4()}/file',
            headers={"Authorization": 'Bearer ' + batch['token']}, files={"file": ("recuerdo.png", data if data is not None else image_bytes(), "image/png")})

    def test_upload_retry_metadata_and_private_original(self):
        batch = self.batch()
        id = str(uuid4())
        result = self.send(batch, id)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['name'], 'María')
        self.assertEqual(result.json()['message'], '¡Qué alegría!')
        self.assertNotIn('driveFileId', result.json())
        self.assertEqual(self.drive.put.call_count, 2)
        self.assertEqual(self.send(batch, id).status_code, 200)
        self.assertEqual(self.drive.put.call_count, 2)
        self.assertEqual(self.drive.generate_ids.call_count, 1)
        self.assertEqual(self.client.get('/api/admin/photos').status_code, 401)
        self.assertEqual(self.client.get(f'/api/admin/photos/{id}/original').status_code, 401)
        login = self.client.post('/api/admin/session', json={"key": "owner-password"})
        auth = {"Authorization": 'Bearer ' + login.json()['token']}
        listing = self.client.get('/api/admin/photos', headers=auth)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()['photos'][0]['id'], id)
        original = self.client.get(f'/api/admin/photos/{id}/original', headers=auth)
        self.assertEqual(original.content, image_bytes())
        self.assertEqual(original.headers['cache-control'], 'private, no-store')
        self.assertIn('attachment', original.headers['content-disposition'])

    def test_batch_tokens_cannot_read_admin_or_upload_another_batch(self):
        batch = self.batch()
        other = self.batch()
        self.assertEqual(self.client.get('/api/admin/photos', headers={"Authorization": 'Bearer '+batch['token']}).status_code, 401)
        other['token'] = batch['token']
        self.assertEqual(self.send(other).status_code, 401)
        self.drive.put.assert_not_called()

    def test_fake_image_size_metadata_and_origin_are_rejected(self):
        batch = self.batch()
        self.assertEqual(self.send(batch, data=b'not a real image').status_code, 415)
        self.assertEqual(self.send(batch, data=b'x'*(MAX_BYTES+1)).status_code, 413)
        self.assertEqual(self.client.post('/api/photo-batches', json={"name": 'x'*101}).status_code, 422)
        self.assertEqual(self.client.post('/api/photo-batches', json={}, headers={"Origin": 'https://evil.example'}).status_code, 403)
        self.drive.put.assert_not_called()

    def test_partial_drive_failure_can_retry_with_same_ids(self):
        batch, id = self.batch(), str(uuid4())
        self.drive.put.side_effect = [None, RuntimeError('private provider details')]
        failed = self.send(batch, id)
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn('private provider details', failed.text)
        self.assertEqual(self.catalog.photos[id]['status'], 'uploading')
        self.drive.put.side_effect = None
        self.assertEqual(self.send(batch, id).status_code, 200)
        self.assertEqual(self.drive.generate_ids.call_count, 1)

    def test_admin_login_rate_limit_and_disabled_backend(self):
        for _ in range(5):
            self.assertEqual(self.client.post('/api/admin/session', json={"key": 'wrong'}).status_code, 401)
        self.assertEqual(self.client.post('/api/admin/session', json={"key": 'owner-password'}).status_code, 429)
        disabled = TestClient(create_app())
        self.assertFalse(disabled.get('/health').json()['uploads_enabled'])
        self.assertEqual(disabled.post('/api/photo-batches', json={}).status_code, 503)

    def test_expired_and_modified_session_tokens_are_rejected(self):
        with patch('time.time', return_value=time.time() - 7201):
            token = self.runtime.issue('owner', 'admin')
        for value in [token, token + 'x']:
            self.assertEqual(self.client.get('/api/admin/photos', headers={"Authorization": 'Bearer '+value}).status_code, 401)


class ImageTests(unittest.TestCase):
    def test_thumbnail_is_jpeg_without_exif_and_original_unchanged(self):
        original = image_bytes()
        checksum = hashlib.sha256(original).hexdigest()
        filename, mime, thumbnail = prepare_image(original, '../../recuerdo.png')
        self.assertEqual(filename, 'recuerdo.png')
        self.assertEqual(mime, 'image/png')
        with Image.open(io.BytesIO(thumbnail)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertFalse(image.getexif())
        self.assertEqual(hashlib.sha256(original).hexdigest(), checksum)
        self.assertEqual(normalize('María ¡ALEGRÍA!'), 'maria ¡alegria!')

    def test_heic_preview_and_mismatched_extension(self):
        from pillow_heif import from_pillow
        stream = io.BytesIO()
        from_pillow(Image.new('RGB', (24, 24), 'red')).save(stream)
        _, mime, thumbnail = prepare_image(stream.getvalue(), 'iphone.heic')
        self.assertEqual(mime, 'image/heic')
        self.assertTrue(thumbnail.startswith(b'\xff\xd8'))
        with self.assertRaises(HTTPException):
            prepare_image(image_bytes(), 'malicious.svg')


class CatalogReservationTests(unittest.TestCase):
    def setUp(self):
        self.data = {}
        self.transaction = Mock()
        self.db = Mock()
        def reference(collection, id):
            ref = Mock()
            ref.get.side_effect = lambda **_: Mock(to_dict=lambda: self.data.get((collection, id)))
            def save(record, merge=False): self.data[(collection, id)] = {**(self.data.get((collection, id), {}) if merge else {}), **record}
            ref.create.side_effect = lambda record, **_: save(record)
            ref.update.side_effect = lambda record, **_: self.data[(collection, id)].update(record)
            ref._save = save
            return ref
        self.db.collection.side_effect = lambda name: Mock(document=lambda id: reference(name, id))
        self.transaction.create.side_effect = lambda ref, record: ref.create(record)
        self.transaction.set.side_effect = lambda ref, record, merge=False: ref._save(record, merge)
        self.transaction.update.side_effect = lambda ref, record: ref.update(record)
        self.db.transaction.return_value = self.transaction
        self.catalog = Catalog(self.db, 1000, 2)
        with patch('app.catalog.firestore.transactional', side_effect=lambda f: f):
            self.catalog.create_batch('batch', 'María', 'Mensaje')

    def claim(self, id='photo', total=100, sha='same'):
        with patch('app.catalog.firestore.transactional', side_effect=lambda f: f):
            return self.catalog.claim('batch', id, 'foto.png', 'image/png', 80, total, sha, ['drive', 'thumb'])

    def test_reservation_is_not_counted_twice_and_limits_apply(self):
        from app.catalog import SYSTEM
        self.claim()
        with self.assertRaises(HTTPException) as result: self.claim()
        self.assertEqual(result.exception.status_code, 409)
        self.catalog.retry_later('photo')
        self.claim()
        self.assertEqual(self.data[(SYSTEM, 'usage')]['bytes'], 100)
        self.catalog.finish('photo')
        self.assertEqual(self.claim()['status'], 'ready')
        self.assertEqual(self.data[(SYSTEM, 'usage')]['photos'], 1)
        self.assertEqual(self.data[(SYSTEM, 'usage')]['batches'], 1)
        with self.assertRaises(HTTPException): self.claim(sha='changed')
        with self.assertRaises(HTTPException) as result: self.claim('other', total=901)
        self.assertEqual(result.exception.status_code, 507)
        self.claim('other')
        with self.assertRaises(HTTPException): self.claim('third')

    def test_batch_cannot_exceed_twenty_photos_or_expiry(self):
        from app.catalog import BATCHES
        self.data[(BATCHES, 'batch')]['photoCount'] = 20
        with self.assertRaises(HTTPException) as result: self.claim()
        self.assertEqual(result.exception.status_code, 400)
        self.data[(BATCHES, 'batch')]['photoCount'] = 0
        self.data[(BATCHES, 'batch')]['expiresAt'] = time.time()-1
        with self.assertRaises(HTTPException) as result: self.claim()
        self.assertEqual(result.exception.status_code, 401)

    def test_empty_batches_also_have_an_album_limit(self):
        from app.catalog import SYSTEM
        self.data[(SYSTEM, 'usage')]['batches'] = 6
        with patch('app.catalog.firestore.transactional', side_effect=lambda f: f):
            with self.assertRaises(HTTPException) as result:
                self.catalog.create_batch('other', '', '')
        self.assertEqual(result.exception.status_code, 507)


if __name__ == '__main__':
    unittest.main()
