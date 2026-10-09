"""Daily encrypted backups of the master database (AES-256-GCM with the master key), 7 kept."""
import base64
import os
import sqlite3
import tempfile


def backup_database(db, crypto, folder, stamp, keep=7):
    os.makedirs(folder, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "copy.db")
        target = sqlite3.connect(copy)
        db._conn().backup(target)   # consistent online copy, even while the service writes
        target.close()
        with open(copy, "rb") as f:
            blob = crypto.encrypt(base64.b64encode(f.read()).decode("ascii"), aad="backup")
    path = os.path.join(folder, f"master-{stamp}.db.enc")
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
    os.replace(path + ".tmp", path)
    backups = sorted(name for name in os.listdir(folder) if name.startswith("master-") and name.endswith(".db.enc"))
    for old in backups[:-keep]:
        os.remove(os.path.join(folder, old))
    return path


def restore_database(path, crypto, destination):
    with open(path, "rb") as f:
        data = base64.b64decode(crypto.decrypt(f.read(), aad="backup"))
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
