import json
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from app.drive_connection import DRIVE_SCOPES, LOCAL_REDIRECT_URI, store_credentials, validate_client_file


class DriveConnectionTests(unittest.TestCase):
    def test_rejects_missing_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.json"
            path.write_text(json.dumps({"web": {"client_id": "id", "client_secret": "secret", "redirect_uris": []}}))
            with self.assertRaises(ValueError):
                validate_client_file(path)
            path.write_text(json.dumps({"web": {"client_id": "id", "client_secret": "secret", "redirect_uris": [LOCAL_REDIRECT_URI]}}))
            validate_client_file(path)

    def test_stores_encrypted_and_preserves_connection_on_incomplete_consent(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "connection.enc"
            key = Fernet.generate_key().decode()
            original = json.dumps({"refresh_token": "test-refresh", "scopes": DRIVE_SCOPES})
            store_credentials(original, key, target)
            ciphertext = target.read_bytes()
            self.assertNotIn(b"test-refresh", ciphertext)
            self.assertEqual(Fernet(key.encode()).decrypt(ciphertext).decode(), original)
            for invalid in [{"scopes": DRIVE_SCOPES}, {"refresh_token": "test-refresh", "scopes": ["https://www.googleapis.com/auth/drive"]}]:
                with self.assertRaises(ValueError):
                    store_credentials(json.dumps(invalid), key, target)
                self.assertEqual(target.read_bytes(), ciphertext)


if __name__ == "__main__":
    unittest.main()
