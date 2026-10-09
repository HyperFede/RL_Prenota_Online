"""Encryption of personal and health data at rest.

- AES-256-GCM with a random 96-bit nonce; the associated data binds a ciphertext to its record,
  so an encrypted codice fiscale copied onto another row does not decrypt.
- Blob format: version (1 byte) | key id (1 byte) | nonce (12 bytes) | ciphertext+tag.
- Key file: JSON {"current": id, "keys": {id: base64 key}}, mode 0600, kept outside the database.
- lookup_hash: keyed HMAC (separate derived key) to find a user by Telegram username/chat id
  without storing them in clear.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import stat

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = 1


class CryptoError(Exception):
    pass


def new_token(nbytes=32):
    """Random URL-safe token (invites, sessions, login requests)."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token):
    """Tokens are stored only as SHA-256: a database leak does not reveal usable sessions."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Crypto:
    def __init__(self, keys, current):
        if current not in keys:
            raise ValueError("La chiave corrente non è nel portachiavi")
        for kid, key in keys.items():
            if not 0 < kid < 256 or len(key) != 32:
                raise ValueError("Ogni chiave deve essere di 32 byte con id 1-255")
        self._aead = {kid: AESGCM(key) for kid, key in keys.items()}
        self.current = current
        hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"rlprenota lookup v1")
        self._lookup_key = hkdf.derive(keys[min(keys)])  # stable across rotations

    @classmethod
    def from_key_file(cls, path):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            raise PermissionError(f"{path} è leggibile da altri utenti (permessi {oct(mode)}): usa chmod 600")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        keys = {int(kid): base64.b64decode(value) for kid, value in data["keys"].items()}
        return cls(keys, int(data["current"]))

    def encrypt(self, plaintext, aad):
        nonce = os.urandom(12)
        body = self._aead[self.current].encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
        return bytes([VERSION, self.current]) + nonce + body

    def decrypt(self, blob, aad):
        if not blob or len(blob) < 2 + 12 + 16 or blob[0] != VERSION:
            raise CryptoError("Dato cifrato non valido")
        aead = self._aead.get(blob[1])
        if aead is None:
            raise CryptoError("Chiave sconosciuta")
        try:
            return aead.decrypt(blob[2:14], blob[14:], aad.encode("utf-8")).decode("utf-8")
        except InvalidTag:
            raise CryptoError("Dato cifrato alterato o associato a un altro record") from None

    def encrypt_json(self, value, aad):
        return self.encrypt(json.dumps(value, separators=(",", ":")), aad)

    def decrypt_json(self, blob, aad):
        return json.loads(self.decrypt(blob, aad))

    def needs_rotation(self, blob):
        return bool(blob) and blob[1] != self.current

    def lookup_hash(self, value):
        normalized = str(value).strip().lstrip("@").lower()
        return hmac.new(self._lookup_key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_key_file(path):
    """Create a new key file (never overwrites). Returns the path."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"current": 1, "keys": {"1": base64.b64encode(os.urandom(32)).decode()}}, f)
    os.chmod(path, 0o600)
    return path
