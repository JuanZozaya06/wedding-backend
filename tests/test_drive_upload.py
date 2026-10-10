"""Exercise the real HTTP/client stack against a local resumable-upload server."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build_from_document
from googleapiclient.discovery_cache import get_static_doc

from app.drive import DriveStore


class DriveUploadTests(unittest.TestCase):
    def setUp(self):
        self.chunks, self.ranges = [], []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, data=None, **headers):
                body = json.dumps(data).encode() if data is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/drive/v3/about"):
                    self.reply(200, {"storageQuota": {}})
                else:
                    self.reply(404, {"error": {"message": "Not found"}})

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.reply(200, Location=test.base + "session")

            def do_PUT(self):
                chunk = self.rfile.read(int(self.headers["Content-Length"]))
                test.chunks.append(chunk)
                test.ranges.append(self.headers["Content-Range"])
                uploaded = sum(map(len, test.chunks))
                total = int(self.headers["Content-Range"].split("/")[-1])
                if uploaded < total:
                    # Drive's 308 means incomplete, with Range and no Location.
                    self.reply(308, Range=f"bytes=0-{uploaded - 1}")
                else:
                    self.reply(200, {"id": "test-original"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}/"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.store = DriveStore.__new__(DriveStore)
        self.store.lock = threading.Lock()
        self.store.credentials = Credentials(token="local-test-only")
        self.store.folder_id, self.store.thumbnails = "album", "previews"
        document = json.loads(get_static_doc("drive", "v3"))
        document.update(rootUrl=self.base, baseUrl=self.base + "drive/v3/")
        self.discovery = json.dumps(document)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def upload(self, data):
        def local_client(_service, _version, *, http, **_kwargs):
            return build_from_document(self.discovery, http=http)

        # Keep the real credentials wrapper, httplib2 and Google upload client.
        with patch("app.drive.build", side_effect=local_client):
            self.store.put("test-original", data, "camera.jpg", "image/jpeg")

    def test_multi_chunk_photo_continues_after_308_without_redirect_location(self):
        data = b"camera-content" * 170_000
        self.upload(data)
        self.assertEqual(b"".join(self.chunks), data)
        self.assertEqual(len(self.chunks), 3)
        self.assertEqual(self.ranges[0], f"bytes 0-1048575/{len(data)}")
        self.assertEqual(self.ranges[1], f"bytes 1048576-2097151/{len(data)}")

    def test_single_chunk_photo_still_uploads(self):
        data = b"small-photo"
        self.upload(data)
        self.assertEqual(self.chunks, [data])


if __name__ == "__main__":
    unittest.main()
