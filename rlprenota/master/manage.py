"""Administration commands (run inside the master container).

  python -m rlprenota.master.manage init <dir>                 create secrets (once; never overwrites)
  python -m rlprenota.master.manage invite NAME --login L [--admin]
  python -m rlprenota.master.manage users
  python -m rlprenota.master.manage resume                    resume searches after a global pause
"""
import argparse
import os
import secrets
import sys

from rlprenota.master.crypto import generate_key_file


def _write_private(path, content):
    if os.path.exists(path):
        return False
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content + "\n")
    return True


def init(base_dir):
    """Create data/ and secrets/ with the master key, the worker secret and the random URL prefix."""
    secrets_dir = os.path.join(base_dir, "secrets")
    os.makedirs(secrets_dir, mode=0o700, exist_ok=True)
    os.makedirs(os.path.join(base_dir, "data"), mode=0o700, exist_ok=True)
    created = []
    key = os.path.join(secrets_dir, "master_key")
    if not os.path.exists(key):
        generate_key_file(key)
        created.append("master_key")
    if _write_private(os.path.join(secrets_dir, "worker_secret"), secrets.token_hex(32)):
        created.append("worker_secret")
    if _write_private(os.path.join(secrets_dir, "url_prefix"), secrets.token_urlsafe(24)):
        created.append("url_prefix")
    return created


def _services(env):
    from rlprenota.master.config import Config
    from rlprenota.master.service import build_services
    config = Config.from_env(env)
    return config, build_services(config)


def main(argv=None, env=None):
    parser = argparse.ArgumentParser(prog="manage")
    sub = parser.add_subparsers(dest="command", required=True)
    p_init = sub.add_parser("init")
    p_init.add_argument("dir")
    p_invite = sub.add_parser("invite")
    p_invite.add_argument("name")
    p_invite.add_argument("--login", required=True)
    p_invite.add_argument("--admin", action="store_true")
    sub.add_parser("users")
    sub.add_parser("resume")
    args = parser.parse_args(argv)

    if args.command == "init":
        created = init(args.dir)
        print("Creati: " + (", ".join(created) if created else "niente (esistevano già)"))
        print("Aggiungi il token del bot in secrets/bot_token (chmod 600).")
        return 0

    config, services = _services(env)
    if args.command == "invite":
        token = services.auth.create_invite(args.name, is_admin=args.admin, login_name=args.login)
        print(f"Invito per {args.name} (nome di accesso: {args.login.lower()}), valido 24 ore:")
        print(config.invite_link(token))
    elif args.command == "users":
        for row in services.db.query("SELECT id, display_name, is_admin, status, last_login_at FROM users ORDER BY id"):
            print(f"{row['id']:>3}  {row['display_name']:<20} {'admin' if row['is_admin'] else '':<6} {row['status']}")
    elif args.command == "resume":
        services.scheduler.resume_global()
        print("Ricerche riprese.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
