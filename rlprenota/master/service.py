"""The master process: one Telegram bot poller, the scheduler's maintenance loop and the web servers."""
import asyncio
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import uvicorn

from rlprenota.core.redact import RedactingFilter
from rlprenota.master.app import Services, create_user_app
from rlprenota.master.auth import AuthService
from rlprenota.master.backup import backup_database
from rlprenota.master.bot import BotGateway
from rlprenota.master.canary import Canary
from rlprenota.master.crypto import Crypto
from rlprenota.master.db import Database
from rlprenota.master.internal_api import create_internal_app
from rlprenota.master.router import ProposalRouter
from rlprenota.master.scheduler import Scheduler
from rlprenota.master.searches import SearchRepo
from rlprenota.telegram import TelegramBot

log = logging.getLogger("rlprenota.master")

REAP_EVERY_SECONDS = 15
EXPIRE_EVERY_SECONDS = 3600
CANARY_EVERY_SECONDS = 30 * 60
BACKUP_HOUR = 4                 # after the NAS antivirus scan (03:00)


def build_services(config):
    db = Database(config.db_path)
    crypto = Crypto.from_key_file(config.key_file)
    gateway = BotGateway(TelegramBot(config.bot_token, "", api_url=config.telegram_api), None, public_url=config.public_url)
    auth = AuthService(db, crypto, gateway)
    gateway.auth = auth
    searches = SearchRepo(db, crypto)
    router = ProposalRouter(db, auth, searches, gateway)
    gateway.decision_handler = router.handle_answer
    scheduler = Scheduler(db, searches, router, workers=config.workers)
    canary = Canary(db, scheduler, router)
    return SimpleNamespace(config=config, db=db, crypto=crypto, gateway=gateway, auth=auth, searches=searches,
                           router=router, scheduler=scheduler, canary=canary)


def bot_loop(services, stop):
    while not stop.is_set():
        if not services.gateway.poll_once(timeout=25):
            stop.wait(10)   # Telegram unreachable: retry calmly


def maintenance_loop(services, stop, clock=time.time):
    last_expire = last_canary = 0.0
    last_backup_day = None
    first_canary = clock() + 60
    while not stop.wait(REAP_EVERY_SECONDS):
        now = clock()
        try:
            services.scheduler.reap_expired()
            if now - last_expire >= EXPIRE_EVERY_SECONDS:
                last_expire = now
                for search_id in services.searches.expire_finished():
                    log.info(f"Ricerca {search_id} conclusa (data di fine superata o inattiva)")
            if now >= first_canary and now - last_canary >= CANARY_EVERY_SECONDS:
                last_canary = now
                result = services.canary.run()
                log.info(f"Canary: {result.status}")
            today = datetime.now()
            if today.hour == BACKUP_HOUR and last_backup_day != today.date():
                last_backup_day = today.date()
                path = backup_database(services.db, services.crypto, os.path.join(services.config.data_dir, "backups"),
                                       today.strftime("%Y%m%d"))
                log.info(f"Backup cifrato creato: {os.path.basename(path)}")
        except Exception:
            log.exception("Errore nel ciclo di manutenzione")


def setup_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for noisy in ("uvicorn.access", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def servers_for(services):
    config = services.config
    user_services = Services(db=services.db, crypto=services.crypto, auth=services.auth, searches=services.searches,
                             trusted_proxies=config.trusted_proxies, public_origin=config.public_origin)
    # Each server listens only on its own Docker network address (see deploy/docker-compose.yml)
    apps = [(create_user_app(user_services, config.url_prefix), config.user_bind, config.user_port),
            (create_internal_app(services.scheduler, services.router, services.searches, config.worker_secret),
             config.internal_bind, config.internal_port)]
    try:
        from rlprenota.master.admin_app import create_admin_app
        apps.append((create_admin_app(services), config.admin_bind, config.admin_port))
    except ImportError:
        pass
    return [uvicorn.Server(uvicorn.Config(app, host=host, port=port, server_header=False, date_header=False,
                                          proxy_headers=False, log_config=None, access_log=False))
            for app, host, port in apps]


def run(config, stop=None):
    setup_logging()
    services = build_services(config)
    stop = stop or threading.Event()
    for target in (bot_loop, maintenance_loop):
        threading.Thread(target=target, args=(services, stop), daemon=True, name=target.__name__).start()
    servers = servers_for(services)

    def shutdown(*_):
        stop.set()
        for server in servers:
            server.should_exit = True
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=lambda: (stop.wait(), shutdown()), daemon=True).start()
    log.info(f"Master avviato ({config.workers} worker previsti)")

    async def serve():
        await asyncio.gather(*(server.serve() for server in servers))
    asyncio.run(serve())
