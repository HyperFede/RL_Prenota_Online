"""Local preview of the user web app with a fake Telegram (codes printed on the console). NOT for production.

python scripts/dev_server.py  ->  http://localhost:8765/devprefix00/  (login name: demo)
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
auth.redeem_invite(auth.create_invite("Demo", login_name="demo"), chat_id=1, username=None)
services = Services(db=db, crypto=crypto, auth=auth, searches=SearchRepo(db, crypto))
app = create_user_app(services, "devprefix00")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, server_header=False)
