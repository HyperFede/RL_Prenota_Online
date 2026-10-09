import os
import stat

import pytest

from rlprenota.master import manage
from rlprenota.master.backup import backup_database, restore_database
from rlprenota.master.config import Config, ConfigError
from rlprenota.master.crypto import Crypto


def make_secrets(tmp_path):
    manage.init(str(tmp_path))
    return tmp_path


def env_for(tmp_path, **extra):
    return {"RLP_DATA_DIR": str(tmp_path / "data"), "RLP_SECRETS_DIR": str(tmp_path / "secrets"),
            "RLP_PUBLIC_ORIGIN": "https://rl-prenota.example.ts.net", "RLP_BOT_USERNAME": "RLPrenotaBot", **extra}


def test_init_creates_private_secrets_once(tmp_path):
    manage.init(str(tmp_path))
    secrets = tmp_path / "secrets"
    for name in ("master_key", "worker_secret", "url_prefix"):
        assert stat.S_IMODE(os.stat(secrets / name).st_mode) == 0o600
    key_before = (secrets / "master_key").read_bytes()
    manage.init(str(tmp_path))                     # idempotent: never regenerates keys
    assert (secrets / "master_key").read_bytes() == key_before
    assert len((secrets / "url_prefix").read_text().strip()) >= 24
    assert (tmp_path / "tailscale").is_dir() and (tmp_path / "data").is_dir()


def test_config_reads_secrets_from_files_only(tmp_path):
    make_secrets(tmp_path)
    (tmp_path / "secrets" / "bot_token").write_text("123:ABC\n")
    os.chmod(tmp_path / "secrets" / "bot_token", 0o600)
    config = Config.from_env(env_for(tmp_path))
    assert config.bot_token == "123:ABC" and len(config.worker_secret) >= 32 and config.url_prefix
    assert config.public_url == f"https://rl-prenota.example.ts.net/{config.url_prefix}/"
    assert config.invite_link("tok") == "https://t.me/RLPrenotaBot?start=tok"


def test_config_refuses_readable_secrets_and_missing_values(tmp_path):
    make_secrets(tmp_path)
    (tmp_path / "secrets" / "bot_token").write_text("123:ABC")
    os.chmod(tmp_path / "secrets" / "bot_token", 0o644)
    with pytest.raises(ConfigError):
        Config.from_env(env_for(tmp_path))
    os.chmod(tmp_path / "secrets" / "bot_token", 0o600)
    with pytest.raises(ConfigError):
        Config.from_env({k: v for k, v in env_for(tmp_path).items() if k != "RLP_PUBLIC_ORIGIN"})
    with pytest.raises(ConfigError):
        Config.from_env(env_for(tmp_path, RLP_PUBLIC_ORIGIN="http://not-https.example"))


def test_encrypted_backup_round_trip_and_rotation(tmp_path, db, crypto):
    db.set_setting("hello", "world")
    folder = tmp_path / "backups"
    for day in range(10):
        path = backup_database(db, crypto, str(folder), stamp=f"2026100{day}", keep=7)
    files = sorted(os.listdir(folder))
    assert len(files) == 7 and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert b"world" not in open(path, "rb").read()          # encrypted at rest
    restored = tmp_path / "restored.db"
    restore_database(path, crypto, str(restored))
    from rlprenota.master.db import Database
    assert Database(str(restored)).get_setting("hello") == "world"
    with pytest.raises(Exception):
        restore_database(path, Crypto({1: os.urandom(32)}, current=1), str(tmp_path / "x.db"))


def test_invite_command_prints_a_link(tmp_path, capsys):
    make_secrets(tmp_path)
    (tmp_path / "secrets" / "bot_token").write_text("123:ABC")
    os.chmod(tmp_path / "secrets" / "bot_token", 0o600)
    env = env_for(tmp_path)
    manage.main(["invite", "Federico", "--login", "fede", "--admin"], env=env)
    out = capsys.readouterr().out
    assert "https://t.me/RLPrenotaBot?start=" in out and "fede" in out


def test_master_process_smoke(tmp_path):
    """The whole master starts: user app under its prefix, bare 404 elsewhere, internal API refuses unsigned calls."""
    import socket
    import threading
    import time
    import urllib.error
    import urllib.request

    from rlprenota.master.service import run
    from tests.fakes import FakeTelegram

    def free_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    make_secrets(tmp_path)
    (tmp_path / "secrets" / "bot_token").write_text("123:ABC")
    os.chmod(tmp_path / "secrets" / "bot_token", 0o600)
    fake = FakeTelegram()
    ports = {"RLP_USER_PORT": str(free_port()), "RLP_ADMIN_PORT": str(free_port()), "RLP_INTERNAL_PORT": str(free_port())}
    config = Config.from_env(env_for(tmp_path, RLP_TELEGRAM_API=fake.url, **ports))
    stop = threading.Event()
    thread = threading.Thread(target=run, args=(config, stop), daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{ports['RLP_USER_PORT']}"
    try:
        for _ in range(100):
            try:
                page = urllib.request.urlopen(f"{base}/{config.url_prefix}/login")
                break
            except OSError:
                time.sleep(0.1)
        assert page.status == 200 and b"Accedi" in page.read()
        with pytest.raises(urllib.error.HTTPError) as hidden:
            urllib.request.urlopen(f"{base}/")
        assert hidden.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as unsigned:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{ports['RLP_INTERNAL_PORT']}/api/lease", data=b"{}"))
        assert unsigned.value.code == 401
    finally:
        stop.set()
        thread.join(10)
        fake.close()
    assert not thread.is_alive()
