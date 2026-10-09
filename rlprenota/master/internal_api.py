"""Internal API between the master and the workers (internal Docker network only, never published).

Every request is signed: HMAC-SHA256 over method, path, timestamp and body hash with a shared secret;
requests older than 30 s or already seen are refused. A worker only ever acts through a lease: the search
is always taken from the server-side lease, never from the request.
"""
import hmac
import json
import threading
import time
from collections import OrderedDict

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from rlprenota.core.models import Slot
from rlprenota.master.scheduler import LeaseLost
from rlprenota.signing import sign

MAX_SKEW_SECONDS = 30
MAX_DECISION_WAIT_SECONDS = 25


class ReplayGuard:
    def __init__(self, ttl=2 * MAX_SKEW_SECONDS, size=10000):
        self._seen = OrderedDict()
        self._lock = threading.Lock()
        self.ttl, self.size = ttl, size

    def first_time(self, signature, now):
        with self._lock:
            while self._seen and (next(iter(self._seen.values())) < now - self.ttl or len(self._seen) > self.size):
                self._seen.popitem(last=False)
            if signature in self._seen:
                return False
            self._seen[signature] = now
            return True


def create_internal_app(scheduler, router, searches, secret, clock=time.time):
    if len(secret) < 32:
        raise ValueError("Il segreto condiviso con i worker deve avere almeno 32 byte")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    replay = ReplayGuard()

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        body = await request.body()
        timestamp = request.headers.get("x-rlp-time", "")
        signature = request.headers.get("x-rlp-signature", "")
        now = clock()
        try:
            fresh = abs(now - float(timestamp)) <= MAX_SKEW_SECONDS
        except ValueError:
            fresh = False
        expected = sign(secret, request.method, request.url.path, timestamp, body)
        if not fresh or not hmac.compare_digest(expected, signature) or not replay.first_time(signature, now):
            return Response(status_code=401)
        return await call_next(request)

    @app.exception_handler(LeaseLost)
    async def lease_lost(request, exc):
        return JSONResponse({"error": "lease_lost"}, status_code=409)

    async def payload(request):
        body = await request.body()
        return json.loads(body) if body else {}

    @app.post("/api/lease")
    async def lease(request: Request):
        data = await payload(request)
        granted = scheduler.lease(str(data.get("worker", "worker"))[:40])
        if granted is None:
            return Response(status_code=204)
        return {"lease_id": granted.lease_id, "search_id": granted.search_id, "settings": json.loads(granted.settings.to_json()),
                "secrets": granted.secrets.to_dict(), "current_appointment": granted.current_appointment,
                "discarded": granted.discarded, "proposals": granted.proposals}

    @app.post("/api/lease/{lease_id}/heartbeat")
    def heartbeat(lease_id: str):
        scheduler.heartbeat(lease_id)
        return {"ok": True}

    @app.post("/api/lease/{lease_id}/permit")
    def permit(lease_id: str):
        scheduler.lease_row(lease_id)
        return {"wait": scheduler.permit()}

    @app.post("/api/lease/{lease_id}/propose")
    async def propose(lease_id: str, request: Request):
        data = await payload(request)
        search_id = scheduler.lease_row(lease_id)["search_id"]
        slot = Slot.from_dict(data["slot"])
        message_id = router.propose(search_id, slot, data.get("current_appointment") or "", bool(data.get("dry_run")),
                                    int(data.get("timeout_minutes") or 15))
        return {"message_id": message_id}

    @app.post("/api/lease/{lease_id}/decision")
    async def decision(lease_id: str, request: Request):
        data = await payload(request)
        search_id = scheduler.lease_row(lease_id)["search_id"]
        wait = min(MAX_DECISION_WAIT_SECONDS, max(0.0, float(data.get("wait") or 0)))
        scheduler.heartbeat(lease_id)
        answer = await anyio.to_thread.run_sync(router.wait_decision, search_id, str(data["slot_id"]), wait)
        return {"decision": answer}

    @app.post("/api/lease/{lease_id}/expire")
    async def expire(lease_id: str, request: Request):
        data = await payload(request)
        router.expire(scheduler.lease_row(lease_id)["search_id"], str(data["slot_id"]))
        return {"ok": True}

    @app.post("/api/lease/{lease_id}/update")
    async def update(lease_id: str, request: Request):
        data = await payload(request)
        router.update(scheduler.lease_row(lease_id)["search_id"], str(data["slot_id"]), str(data.get("footer", ""))[:2000],
                      bool(data.get("keep_buttons")))
        return {"ok": True}

    @app.post("/api/lease/{lease_id}/notify")
    async def notify(lease_id: str, request: Request):
        data = await payload(request)
        row = scheduler.lease_row(lease_id)
        router.notify_user(row["user_id"], str(data.get("text", ""))[:1000])
        return {"ok": True}

    @app.post("/api/lease/{lease_id}/store")
    async def store(lease_id: str, request: Request):
        data = await payload(request)
        decisions = searches.decisions(scheduler.lease_row(lease_id)["search_id"])
        op, slot_id = data.get("op"), str(data.get("slot_id", ""))
        if op == "status":
            if slot_id not in decisions.proposals and data.get("slot"):
                decisions.record_proposal(Slot.from_dict(data["slot"]), data.get("message_id"))
            decisions.set_status(slot_id, str(data["status"]))
        elif op == "discard":
            decisions.discard(Slot.from_dict(data["slot"]))
        elif op == "undiscard":
            decisions.undiscard(slot_id)
        else:
            return JSONResponse({"error": "op"}, status_code=400)
        return {"ok": True}

    @app.post("/api/lease/{lease_id}/complete")
    async def complete(lease_id: str, request: Request):
        data = await payload(request)
        scheduler.complete(lease_id, outcome=str(data.get("outcome")), duration=float(data.get("duration") or 0),
                           error_kind=data.get("error_kind"), message=str(data.get("message") or "")[:300],
                           selector=str(data.get("selector") or "")[:200], step=str(data.get("step") or "")[:200],
                           current_appointment=data.get("current_appointment"))
        return {"ok": True}

    return app
