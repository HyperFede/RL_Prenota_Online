"""Worker process: RLP_MASTER_URL, RLP_SECRETS_DIR (worker_secret), optional RLP_WORKER_NAME."""
import logging
import os
import signal
import socket
import sys
import threading

from rlprenota.core.redact import RedactingFilter
from rlprenota.master.config import read_secret
from rlprenota.worker.browser import BrowserManager
from rlprenota.worker.client import MasterClient
from rlprenota.worker.worker import Worker


def main():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    logging.getLogger().handlers[:] = [handler]
    logging.getLogger().setLevel(logging.INFO)

    secret = bytes.fromhex(read_secret(os.path.join(os.environ.get("RLP_SECRETS_DIR", "/run/secrets"), "worker_secret")))
    name = os.environ.get("RLP_WORKER_NAME") or socket.gethostname()
    browsers = BrowserManager()
    worker = Worker(MasterClient(os.environ.get("RLP_MASTER_URL", "http://master:8002"), secret, name), browsers, name=name)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    logging.getLogger("rlprenota.worker").info(f"Worker {name} avviato")
    try:
        worker.run_forever(stop)
    finally:
        browsers.kill()


if __name__ == "__main__":
    main()
