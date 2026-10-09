"""Local preview of the web apps with a fake Telegram (logins auto-approved). NOT for production.

python scripts/dev_server.py
  user app:  http://localhost:8765/devprefix00/   (login name: demo)
  admin:     http://localhost:8766/admin/         (login name: admin)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn  # noqa: E402

from rlprenota.master.app import Services, create_user_app  # noqa: E402
from rlprenota.master.auth import AuthService  # noqa: E402
from rlprenota.master.crypto import Crypto  # noqa: E402
from rlprenota.master.db import Database  # noqa: E402
from rlprenota.master.searches import SearchRepo  # noqa: E402


class PrintingNotifier:
    def __init__(self):
        self.auth = None

    def send_login_request(self, chat_id, request_id, code, device):
        print(f"[fake Telegram] login from {device}: auto-approved (code {code})", flush=True)
        self.auth.approve(request_id, chat_id)


db = Database(os.path.join(tempfile.mkdtemp(), "dev.db"))
crypto = Crypto({1: os.urandom(32)}, current=1)
notifier = PrintingNotifier()
auth = AuthService(db, crypto, notifier)
notifier.auth = auth
demo = auth.redeem_invite(auth.create_invite("Demo", login_name="demo"), chat_id=1, username=None)
auth.redeem_invite(auth.create_invite("Admin", login_name="admin", is_admin=True), chat_id=2, username=None)
searches = SearchRepo(db, crypto)
services = Services(db=db, crypto=crypto, auth=auth, searches=searches)
app = create_user_app(services, "devprefix00")

# Admin console with a sample search
from datetime import date  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from rlprenota.master.admin_app import create_admin_app  # noqa: E402
from rlprenota.master.canary import CanaryResult  # noqa: E402
from rlprenota.master.scheduler import Scheduler  # noqa: E402
from rlprenota.master.validation import SearchSecrets, SearchSettings  # noqa: E402

searches.create(demo, SearchSettings.from_form({"label": "Visita cardiologica", "location_mode": "radius", "home_comune": "Milano",
                                                "max_km": "25", "end_date": "2099-12-31"}, today=date.today()),
                SearchSecrets.from_form({"codice_fiscale": "MRTMTT25D09F205Z", "tessera": "12345", "ricetta": "0300A1234567890"}))


class LogAlerts:
    def notify_user(self, user_id, text):
        print("[notify]", user_id, text)

    def alert_admin(self, key, text):
        print("[admin]", key, text)


admin_services = SimpleNamespace(db=db, crypto=crypto, auth=auth, searches=searches, scheduler=Scheduler(db, searches, LogAlerts()),
                                 canary=SimpleNamespace(run=lambda: CanaryResult("ok", "demo")),
                                 config=SimpleNamespace(invite_link=lambda t: f"https://t.me/DemoBot?start={t}", workers=2,
                                                        admin_origin="http://localhost:8766", trusted_proxies=()))
admin_app = create_admin_app(admin_services)

if __name__ == "__main__":
    import asyncio

    async def serve():
        servers = [uvicorn.Server(uvicorn.Config(a, host="127.0.0.1", port=p, server_header=False))
                   for a, p in ((app, 8765), (admin_app, 8766))]
        await asyncio.gather(*(s.serve() for s in servers))
    asyncio.run(serve())
