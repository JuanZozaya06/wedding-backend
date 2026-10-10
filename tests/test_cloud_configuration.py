import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.drive import DriveStore
from app.drive_connection import DRIVE_SCOPES
from app.main import Runtime, configured_runtime, create_app


class CloudConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key()
        self.info = {"scopes": DRIVE_SCOPES, "refresh_token": "test-refresh-token"}
        self.env = {
            "DRIVE_CREDENTIALS_ENCRYPTED": self.encrypt(self.info),
            "GOOGLE_DRIVE_THUMBNAILS_FOLDER_ID": "thumbnail-folder",
        }
        self.credentials = Mock(valid=True)
        self.drive = MagicMock()
        self.drive.__enter__.return_value = self.drive
        self.drive.files.return_value.get.return_value.execute.side_effect = [
            {"mimeType": "application/vnd.google-apps.folder", "capabilities": {"canAddChildren": True}},
            {"mimeType": "application/vnd.google-apps.folder", "parents": ["album-folder"]},
        ]

    def encrypt(self, info):
        return Fernet(self.key).encrypt(json.dumps(info).encode()).decode()

    def open_store(self, root):
        with patch.dict(os.environ, self.env, clear=True), \
             patch("app.drive.Credentials.from_authorized_user_info", return_value=self.credentials), \
             patch("app.drive.build", return_value=self.drive):
            return DriveStore(root, self.key.decode(), "album-folder")

    def test_environment_credentials_need_no_local_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.open_store(root)
            self.assertFalse(store.local_credentials)
            self.assertEqual(store.thumbnails, "thumbnail-folder")
            self.assertEqual(list(root.iterdir()), [])
            self.drive.files.return_value.create.assert_not_called()

    def test_environment_mode_requires_existing_thumbnail_folder(self):
        self.env["GOOGLE_DRIVE_THUMBNAILS_FOLDER_ID"] = ""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                self.open_store(Path(directory))
            self.drive.files.assert_not_called()

    def test_broader_drive_scope_is_rejected(self):
        self.env["DRIVE_CREDENTIALS_ENCRYPTED"] = self.encrypt({**self.info, "scopes": ["https://www.googleapis.com/auth/drive"]})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                self.open_store(Path(directory))
            self.drive.files.assert_not_called()

    def test_cloud_token_refresh_does_not_write_to_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.open_store(root)
            self.credentials.valid = False
            with patch("app.drive.build"), patch("app.drive.Request"), patch("app.drive.store_credentials") as persist:
                store.client()
            self.credentials.refresh.assert_called_once()
            persist.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])

    def test_firebase_json_project_mismatch_fails_before_connecting(self):
        env = {"PHOTOS_ENABLED": "true", "GOOGLE_CLOUD_PROJECT": "wedding-project", "FIREBASE_SERVICE_ACCOUNT_JSON": '{"project_id":"another-project"}'}
        with patch.dict(os.environ, env, clear=True), \
             patch("app.main.credentials.Certificate", return_value=Mock(project_id="another-project")) as certificate, \
             patch("app.main.firestore.client") as database:
            with self.assertRaises(ValueError):
                configured_runtime()
            certificate.assert_called_once_with({"project_id": "another-project"})
            database.assert_not_called()

    def test_readiness_requires_active_connections(self):
        with patch.dict(os.environ, {"PHOTOS_ENABLED": "false"}, clear=True):
            with TestClient(create_app()) as disabled:
                self.assertEqual(disabled.get("/health").status_code, 200)
                self.assertEqual(disabled.get("/ready").status_code, 503)
            with TestClient(create_app(Runtime(Mock(), Mock(), "test-key"))) as enabled:
                self.assertEqual(enabled.get("/ready").status_code, 200)


if __name__ == "__main__":
    unittest.main()
