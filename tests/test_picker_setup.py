import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from select_drive_folder import FOLDER_MIME, ORIGIN, PickerSetup, make_handler, validate_folder


class PickerTests(unittest.TestCase):
    def test_folder_must_match_allow_upload_and_not_be_trashed(self):
        valid = {"id": "wedding", "mimeType": FOLDER_MIME, "capabilities": {"canAddChildren": True}}
        validate_folder(valid, "wedding")
        for folder in [dict(valid, id="other"), dict(valid, trashed=True),
                       dict(valid, mimeType="image/jpeg"), dict(valid, capabilities={})]:
            with self.assertRaises(ValueError):
                validate_folder(folder, "wedding")

    def test_wrong_folder_never_calls_google_or_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            setup = PickerSetup(Mock(), "key", "api-key", "wedding", Path(directory))
            with patch("select_drive_folder.AuthorizedSession") as session:
                with self.assertRaises(ValueError):
                    setup.select("other")
                session.assert_not_called()
            self.assertFalse(setup.completed)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_verified_folder_saves_only_reference_and_encrypted_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            setup = PickerSetup(Mock(), "key", "api-key", "wedding", Path(directory))
            with patch("select_drive_folder.AuthorizedSession") as session, patch("select_drive_folder.store_credentials") as store:
                response = session.return_value.__enter__.return_value.get.return_value
                response.status_code = 200
                response.json.return_value = {"id": "wedding", "mimeType": FOLDER_MIME, "capabilities": {"canAddChildren": True}}
                setup.select("wedding")
                store.assert_called_once()
            saved = json.loads((Path(directory) / ".secrets/drive-folder.json").read_text())
            self.assertEqual(set(saved), {"id", "verifiedAt"})
            self.assertTrue(setup.completed)

    def test_loopback_routes_need_session_host_origin_and_single_use_bootstrap(self):
        setup = PickerSetup(Mock(), "key", "api-key", "wedding")
        setup.config = Mock(return_value={"token": "private"})
        setup.select = Mock(return_value={"message": "ok"})
        with HTTPServer(("127.0.0.1", 0), make_handler(setup)) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(method, path, headers=None, body=None):
                connection = HTTPConnection(*server.server_address)
                connection.request(method, path, headers=headers or {"Host": "127.0.0.1:8766"}, body=body)
                result = connection.getresponse()
                status, data, response_headers = result.status, result.read(), dict(result.getheaders())
                connection.close()
                return status, data, response_headers

            try:
                self.assertEqual(request("GET", "/config")[0], 403)
                setup.config.assert_not_called()
                bootstrap = "/setup/" + setup.bootstrap
                status, _, headers = request("GET", bootstrap)
                self.assertEqual(status, 303)
                self.assertIn("HttpOnly", headers["Set-Cookie"])
                self.assertEqual(request("GET", bootstrap)[0], 403)
                authenticated = {"Host": "127.0.0.1:8766", "Cookie": "picker_setup=" + setup.session}
                status, data, headers = request("GET", "/config", authenticated)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(data)["token"], "private")
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertNotIn("Access-Control-Allow-Origin", headers)
                self.assertEqual(request("GET", "/config", dict(authenticated, Host="evil.test"))[0], 403)
                self.assertEqual(request("POST", "/select", authenticated, '{"id":"wedding"}')[0], 403)
                self.assertEqual(request("POST", "/select", dict(authenticated, Origin="https://evil.test"), '{"id":"wedding"}')[0], 403)
                setup.select.assert_not_called()
                self.assertEqual(request("POST", "/select", dict(authenticated, Origin=ORIGIN), '{"id":"wedding"}')[0], 200)
                setup.select.assert_called_once_with("wedding")
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
