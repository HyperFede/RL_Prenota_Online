import base64
import os
import stat

import pytest

from rlprenota.master.crypto import Crypto, CryptoError, generate_key_file, hash_token, new_token


@pytest.fixture
def crypto():
    return Crypto({1: os.urandom(32)}, current=1)


def test_round_trip_text_and_json(crypto):
    blob = crypto.encrypt("RSSMRA80A01F205X", aad="search:42")
    assert b"RSSMRA80A01F205X" not in blob
    assert crypto.decrypt(blob, aad="search:42") == "RSSMRA80A01F205X"
    secrets = {"cf": "RSSMRA80A01F205X", "tessera": "12345"}
    assert crypto.decrypt_json(crypto.encrypt_json(secrets, aad="s:1"), aad="s:1") == secrets


def test_same_plaintext_gives_different_ciphertexts(crypto):
    assert crypto.encrypt("x", aad="a") != crypto.encrypt("x", aad="a")


def test_wrong_aad_is_rejected(crypto):
    blob = crypto.encrypt("secret", aad="search:42")
    with pytest.raises(CryptoError):
        crypto.decrypt(blob, aad="search:43")  # copied onto another record


def test_tampering_is_detected(crypto):
    blob = bytearray(crypto.encrypt("secret", aad="a"))
    blob[-1] ^= 1
    with pytest.raises(CryptoError):
        crypto.decrypt(bytes(blob), aad="a")


def test_key_rotation_reads_old_data():
    old_key, new_key = os.urandom(32), os.urandom(32)
    old = Crypto({1: old_key}, current=1)
    blob = old.encrypt("vecchio", aad="a")
    rotated = Crypto({1: old_key, 2: new_key}, current=2)
    assert rotated.decrypt(blob, aad="a") == "vecchio"
    assert rotated.decrypt(rotated.encrypt("nuovo", aad="a"), aad="a") == "nuovo"
    assert rotated.needs_rotation(blob) and not rotated.needs_rotation(rotated.encrypt("n", aad="a"))
    with pytest.raises(CryptoError):
        Crypto({2: new_key}, current=2).decrypt(blob, aad="a")  # unknown key id


def test_lookup_hash_is_stable_normalized_and_keyed(crypto):
    assert crypto.lookup_hash("@Fede_M") == crypto.lookup_hash("fede_m")
    other = Crypto({1: os.urandom(32)}, current=1)
    assert crypto.lookup_hash("fede_m") != other.lookup_hash("fede_m")


def test_invalid_keys_rejected():
    with pytest.raises(ValueError):
        Crypto({1: b"short"}, current=1)
    with pytest.raises(ValueError):
        Crypto({1: os.urandom(32)}, current=2)


def test_key_file_is_private(tmp_path):
    path = tmp_path / "master.key"
    generate_key_file(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    loaded = Crypto.from_key_file(path)
    assert loaded.decrypt(loaded.encrypt("x", aad="a"), aad="a") == "x"
    with pytest.raises(FileExistsError):
        generate_key_file(path)  # never overwrite an existing key
    os.chmod(path, 0o644)
    with pytest.raises(PermissionError):
        Crypto.from_key_file(path)  # refuses a key readable by others


def test_tokens():
    token = new_token()
    assert len(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))) == 32
    assert new_token() != token
    assert hash_token(token) == hash_token(token) and hash_token(token) != token
