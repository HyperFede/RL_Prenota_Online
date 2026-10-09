"""Master configuration: plain settings from environment variables, secrets only from 0600 files."""
import os
import stat
from dataclasses import dataclass
from urllib.parse import urlsplit


class ConfigError(Exception):
    pass


def read_secret(path, binary=False):
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        raise ConfigError(f"File segreto mancante: {path}") from None
    if mode & 0o077:
        raise ConfigError(f"{path} è leggibile da altri utenti (permessi {oct(mode)}): usa chmod 600")
    with open(path, "rb") as f:
        data = f.read().strip()
    if not data:
        raise ConfigError(f"File segreto vuoto: {path}")
    return data if binary else data.decode("utf-8")


@dataclass
class Config:
    data_dir: str
    secrets_dir: str
    public_origin: str
    bot_username: str
    bot_token: str
    worker_secret: bytes
    url_prefix: str
    workers: int = 2
    trusted_proxies: tuple = ()
    user_port: int = 8000
    admin_bind: str = "127.0.0.1"
    admin_port: int = 8001
    internal_port: int = 8002
    telegram_api: str = "https://api.telegram.org"
    admin_origin: str = ""     # tailnet-only HTTPS origin of the admin console (Tailscale serve, port 8443)

    @property
    def db_path(self):
        return os.path.join(self.data_dir, "master.db")

    @property
    def key_file(self):
        return os.path.join(self.secrets_dir, "master_key")

    @property
    def public_url(self):
        return f"{self.public_origin.rstrip('/')}/{self.url_prefix}/"

    def invite_link(self, token):
        return f"https://t.me/{self.bot_username}?start={token}"

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        secrets_dir = env.get("RLP_SECRETS_DIR", "/run/secrets")
        origin = env.get("RLP_PUBLIC_ORIGIN", "").strip()
        if not origin:
            raise ConfigError("RLP_PUBLIC_ORIGIN mancante (es. https://rl-prenota.<tailnet>.ts.net)")
        parts = urlsplit(origin)
        if parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/"):
            raise ConfigError("RLP_PUBLIC_ORIGIN deve essere un'origine https senza percorso")
        bot_username = env.get("RLP_BOT_USERNAME", "").strip().lstrip("@")
        if not bot_username:
            raise ConfigError("RLP_BOT_USERNAME mancante")
        worker_secret = bytes.fromhex(read_secret(os.path.join(secrets_dir, "worker_secret")))
        if len(worker_secret) < 32:
            raise ConfigError("worker_secret troppo corto")
        return cls(
            data_dir=env.get("RLP_DATA_DIR", "/data"), secrets_dir=secrets_dir, public_origin=origin.rstrip("/"),
            bot_username=bot_username, bot_token=read_secret(os.path.join(secrets_dir, "bot_token")),
            worker_secret=worker_secret, url_prefix=read_secret(os.path.join(secrets_dir, "url_prefix")),
            workers=int(env.get("RLP_WORKERS", "2")),
            trusted_proxies=tuple(p.strip() for p in env.get("RLP_TRUSTED_PROXIES", "").split(",") if p.strip()),
            user_port=int(env.get("RLP_USER_PORT", "8000")), admin_bind=env.get("RLP_ADMIN_BIND", "127.0.0.1"),
            admin_port=int(env.get("RLP_ADMIN_PORT", "8001")), internal_port=int(env.get("RLP_INTERNAL_PORT", "8002")),
            telegram_api=env.get("RLP_TELEGRAM_API", "https://api.telegram.org"),
            admin_origin=env.get("RLP_ADMIN_ORIGIN", f"{origin.rstrip('/')}:8443"))
