"""Admin console: reachable only inside the tailnet (served by the Tailscale sidecar, never funneled),
and only for admin accounts (same Telegram login). It never shows personal data of the searches."""
import json
from types import SimpleNamespace

from fastapi import Request
from fastapi.responses import Response

from rlprenota.master.app import Services, bare_404, create_base_app
from rlprenota.master.auth import AuthError

BASE = "/admin"
MAX_LIMIT_PER_USER = 10
MAX_LIMIT_TOTAL = 30


def create_admin_app(services):
    config = services.config
    shared = create_base_app(Services(db=services.db, crypto=services.crypto, auth=services.auth, searches=services.searches,
                                      trusted_proxies=config.trusted_proxies, public_origin=config.admin_origin),
                             BASE, allow=lambda info: info.is_admin)
    app, render, redirect, require_user, form_data = shared.app, shared.render, shared.redirect, shared.require_user, shared.form_data
    db, auth, searches, scheduler = services.db, services.auth, services.searches, services.scheduler

    def overview(request, user, status=200, **extra):
        users = db.query("""SELECT u.id, u.display_name, u.is_admin, u.status, u.last_login_at,
                                   (SELECT COUNT(*) FROM searches s WHERE s.user_id = u.id) AS searches
                            FROM users u ORDER BY u.id""")
        rows = db.query("""SELECT s.id, s.user_id, u.display_name AS owner, s.status, s.settings_json, s.last_run_at,
                                  s.last_result, s.consecutive_failures, s.effective_interval
                           FROM searches s JOIN users u ON u.id = s.user_id ORDER BY s.created_at DESC LIMIT 200""")
        all_searches = [SimpleNamespace(**dict(r), label=json.loads(r["settings_json"])["label"]) for r in rows]
        capacity = SimpleNamespace(
            active=db.query_one("SELECT COUNT(*) AS n FROM searches WHERE status IN ('active', 'waiting')")["n"],
            leases=db.query_one("SELECT COUNT(*) AS n FROM leases")["n"],
            per_user=db.get_setting("max_active_per_user", 3), total=db.get_setting("max_active_total", 10),
            avg_cycle=float(db.get_setting("avg_cycle_seconds", 0) or 0), workers=config.workers)
        canary = SimpleNamespace(status=db.get_setting("canary_status", "mai eseguito"), detail=db.get_setting("canary_detail", ""),
                                 at=float(db.get_setting("canary_at", 0) or 0))
        audit = db.query("SELECT ts, user_id, action, detail FROM audit ORDER BY id DESC LIMIT 40")
        return render(request, "admin.html", status=status, user=user, users=users, searches=all_searches, capacity=capacity,
                      canary=canary, audit=audit, paused=db.get_setting("global_pause", ""),
                      pause_until=float(db.get_setting("pause_until", 0) or 0), **extra)

    async def admin_form(request):
        form = await form_data(request)
        return form, require_user(request, form)

    @app.get(f"{BASE}/")
    @app.get(BASE)
    async def home(request: Request):
        user = require_user(request)
        if isinstance(user, Response):
            return user
        return overview(request, user)

    @app.post(f"{BASE}/invite")
    async def invite(request: Request):
        form, user = await admin_form(request)
        if isinstance(user, Response):
            return user
        try:
            token = auth.create_invite((form.get("name") or "").strip(), is_admin=form.get("admin") == "on",
                                       login_name=(form.get("login") or "").strip())
        except AuthError as e:
            return overview(request, user, status=422, error=str(e))
        return overview(request, user, invite={"link": config.invite_link(token), "login": (form.get("login") or "").strip().lower(),
                                               "name": form.get("name")})

    @app.post(f"{BASE}/users/{{user_id}}/{{action}}")
    async def user_action(request: Request, user_id: int, action: str):
        form, user = await admin_form(request)
        if isinstance(user, Response):
            return user
        if action not in ("disable", "enable") or db.query_one("SELECT 1 FROM users WHERE id = ?", (user_id,)) is None:
            return bare_404()
        if action == "disable":
            if user_id == user.user_id:
                return overview(request, user, status=422, error="Non puoi disattivare te stesso")
            auth.set_user_status(user_id, "disabled")
            for row in db.query("SELECT id FROM searches WHERE user_id = ? AND status IN ('active', 'waiting')", (user_id,)):
                searches.pause(user_id, row["id"])
        else:
            auth.set_user_status(user_id, "active")
        return redirect("")

    @app.post(f"{BASE}/searches/{{search_id}}/{{action}}")
    async def search_action(request: Request, search_id: str, action: str):
        form, user = await admin_form(request)
        if isinstance(user, Response):
            return user
        row = db.query_one("SELECT user_id FROM searches WHERE id = ?", (search_id,))
        if row is None or action not in ("pause", "resume"):
            return bare_404()
        try:
            getattr(searches, action)(row["user_id"], search_id)
        except Exception as e:
            return overview(request, user, status=409, error=str(e))
        return redirect("")

    @app.post(f"{BASE}/global/{{action}}")
    async def global_action(request: Request, action: str):
        form, user = await admin_form(request)
        if isinstance(user, Response):
            return user
        if action == "resume":
            scheduler.resume_global()
        elif action == "pause":
            scheduler.pause_global(f"Sospese dall'amministratore: {(form.get('reason') or '').strip()[:200]}")
        else:
            return bare_404()
        db.audit(f"global_{action}", user.user_id)
        return redirect("")

    @app.post(f"{BASE}/canary/run")
    async def canary_run(request: Request):
        form, user = await admin_form(request)
        if isinstance(user, Response):
            return user
        services.canary.run()
        return redirect("")

    @app.post(f"{BASE}/limits")
    async def limits(request: Request):
        form, user = await admin_form(request)
        if isinstance(user, Response):
            return user
        try:
            per_user, total = int(form.get("per_user") or ""), int(form.get("total") or "")
        except ValueError:
            per_user = total = 0
        if not (1 <= per_user <= MAX_LIMIT_PER_USER and per_user <= total <= MAX_LIMIT_TOTAL):
            return overview(request, user, status=422,
                            error=f"Limiti non validi (per utente 1-{MAX_LIMIT_PER_USER}, totale fino a {MAX_LIMIT_TOTAL})")
        db.set_setting("max_active_per_user", per_user)
        db.set_setting("max_active_total", total)
        db.audit("limits_changed", user.user_id, f"{per_user}/{total}")
        return redirect("")

    return app
