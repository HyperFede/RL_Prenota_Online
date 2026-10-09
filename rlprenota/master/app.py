"""User web app (reachable from the internet through Tailscale Funnel, under a random path prefix).

No JavaScript at all (CSP script-src 'none'): server-rendered pages, the login page refreshes itself.
Everything outside the prefix, and every missing or foreign resource, is a bare 404.
"""
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from rlprenota.master.auth import AuthError
from rlprenota.master.searches import LimitReached, NotFound
from rlprenota.master.validation import PROVINCES, SearchSecrets, SearchSettings, ValidationError

HERE = os.path.dirname(__file__)
SESSION_COOKIE = "rlp_s"
LOGIN_COOKIE = "rlp_l"
SESSION_MAX_AGE = 30 * 86400
LOGIN_MAX_AGE = 5 * 60
SECRET_FIELDS = ("codice_fiscale", "tessera", "ricetta", "telefono", "email")
WEEKDAYS = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
STATUS_LABELS = {"active": "Attiva", "waiting": "In attesa della tua risposta", "paused": "In pausa",
                 "error": "Richiede la tua attenzione", "booked": "Prenotata", "expired": "Conclusa"}

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'; script-src 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # "no-referrer" would make browsers send "Origin: null" on form posts, breaking the same-origin check;
    # "same-origin" still never sends the referrer to other sites.
    "Referrer-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}


@dataclass
class Services:
    db: object
    crypto: object
    auth: object
    searches: object
    clock: object = time.time
    trusted_proxies: tuple = ()
    public_origin: str = ""          # e.g. https://rl-prenota.<tailnet>.ts.net (empty: same as the request)
    extra: dict = field(default_factory=dict)


def device_label(user_agent):
    ua = user_agent or ""
    browser = next((name for key, name in [("Edg/", "Edge"), ("Firefox/", "Firefox"), ("OPR/", "Opera"),
                                           ("Chrome/", "Chrome"), ("CriOS", "Chrome"), ("Safari", "Safari")] if key in ua), "Browser")
    system = next((name for key, name in [("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                          ("Mac OS X", "Mac"), ("Macintosh", "Mac"), ("Windows", "Windows"), ("Linux", "Linux")]
                   if key in ua), "dispositivo sconosciuto")
    return f"{browser} su {system}"


def bare_404():
    return Response(status_code=404)


def create_user_app(services, prefix):
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", prefix):
        raise ValueError("Il prefisso deve essere casuale (8-64 caratteri URL-safe)")
    base = f"/{prefix}"
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
    from rlprenota.geo.distance import ComuniIndex
    comuni = ComuniIndex.load().names()
    templates.env.globals.update(base=base, weekdays=WEEKDAYS, provinces=PROVINCES, status_labels=STATUS_LABELS, comuni=comuni)
    templates.env.filters["when"] = lambda ts: datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M") if ts else "—"
    templates.env.filters["itdate"] = lambda iso: datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y") if iso else ""
    app.mount(f"{base}/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
    auth, searches = services.auth, services.searches

    # --- middleware: hiding, origin check, headers ---

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path != base and not path.startswith(base + "/"):
            return bare_404()
        if request.method not in ("GET", "HEAD") and not same_origin(request):
            response = Response("Richiesta da un'origine non consentita", status_code=403)
        else:
            response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if "server" in response.headers:
            del response.headers["server"]
        return response

    def same_origin(request):
        origin = request.headers.get("origin") or ""
        expected = services.public_origin or f"{request.url.scheme}://{request.url.netloc}"
        return bool(origin) and origin.rstrip("/") == expected.rstrip("/")

    @app.exception_handler(StarletteHTTPException)
    async def http_errors(request, exc):
        return bare_404() if exc.status_code in (404, 405) else Response(status_code=exc.status_code)

    @app.exception_handler(NotFound)
    async def not_found(request, exc):
        return bare_404()

    # --- helpers ---

    def client_ip(request):
        peer = request.client.host if request.client else ""
        if peer in services.trusted_proxies:
            return (request.headers.get("x-forwarded-for") or peer).split(",")[0].strip()
        return peer

    def current(request):
        return auth.session(request.cookies.get(SESSION_COOKIE))

    def render(request, name, status=200, **context):
        return templates.TemplateResponse(request, name, {"user": context.pop("user", None), **context}, status_code=status)

    def redirect(path):
        return RedirectResponse(f"{base}/{path}", status_code=303)

    def set_cookie(response, name, value, max_age, samesite):
        response.set_cookie(name, value, max_age=max_age, path=base, secure=True, httponly=True, samesite=samesite)

    async def form_data(request):
        form = await request.form()
        return {key: (form.getlist(key) if key in ("province", "weekdays") else form.get(key)) for key in form.keys()}

    def require_user(request, form=None):
        """Session + CSRF check for POSTs. Returns the SessionInfo or a Response."""
        user = current(request)
        if user is None:
            return redirect("login")
        if form is not None and not user.check_csrf(form.get("csrf")):
            return Response("Sessione scaduta: ricarica la pagina", status_code=403)
        return user

    # --- login ---

    @app.get(f"{base}/login")
    async def login_page(request: Request):
        if current(request):
            return redirect("")
        return render(request, "login.html")

    @app.post(f"{base}/login")
    async def login_start(request: Request):
        form = await form_data(request)
        try:
            browser = auth.start_login(form.get("username") or "", device_label(request.headers.get("user-agent")),
                                       client=client_ip(request))
        except AuthError as e:
            return render(request, "login.html", status=429, error=str(e))
        response = redirect("login/wait")
        set_cookie(response, LOGIN_COOKIE, browser, LOGIN_MAX_AGE, "strict")
        return response

    def finish_login(request, browser):
        reauth_session = request.cookies.get(SESSION_COOKIE) if request.cookies.get("rlp_r") else None
        if reauth_session:
            auth.complete_reauth(browser, reauth_session)
            response = RedirectResponse(request.cookies.get("rlp_r"), status_code=303)
            response.delete_cookie("rlp_r", path=base)
        else:
            session = auth.complete_login(browser)
            response = redirect("")
            set_cookie(response, SESSION_COOKIE, session, SESSION_MAX_AGE, "lax")
        response.delete_cookie(LOGIN_COOKIE, path=base)
        return response

    @app.get(f"{base}/login/wait")
    async def login_wait(request: Request):
        browser = request.cookies.get(LOGIN_COOKIE)
        status = auth.login_status(browser)
        if status == "approved":
            try:
                return finish_login(request, browser)
            except AuthError:
                status = "expired"
        return render(request, "login_wait.html", status=200, login_status=status, reauth=bool(request.cookies.get("rlp_r")))

    @app.post(f"{base}/login/code")
    async def login_code(request: Request):
        form = await form_data(request)
        browser = request.cookies.get(LOGIN_COOKIE)
        if auth.check_code(browser, (form.get("code") or "").strip()):
            try:
                return finish_login(request, browser)
            except AuthError:
                pass
        return render(request, "login_wait.html", status=400, login_status=auth.login_status(browser),
                      error="Codice non valido", reauth=bool(request.cookies.get("rlp_r")))

    @app.post(f"{base}/logout")
    async def logout(request: Request):
        user = require_user(request, await form_data(request))
        if isinstance(user, Response):
            return user
        auth.revoke(request.cookies.get(SESSION_COOKIE))
        response = redirect("login")
        response.delete_cookie(SESSION_COOKIE, path=base)
        return response

    @app.post(f"{base}/confirm")
    async def confirm_identity(request: Request):
        """Ask for a Telegram confirmation before a sensitive change (then come back to `next`)."""
        form = await form_data(request)
        user = require_user(request, form)
        if isinstance(user, Response):
            return user
        next_path = form.get("next") or ""
        if not next_path.startswith(base + "/") or "//" in next_path[len(base):]:
            next_path = f"{base}/"
        browser = auth.start_reauth(user.user_id, device_label(request.headers.get("user-agent")))
        response = redirect("login/wait")
        set_cookie(response, LOGIN_COOKIE, browser, LOGIN_MAX_AGE, "strict")
        set_cookie(response, "rlp_r", next_path, LOGIN_MAX_AGE, "strict")
        return response

    # --- pages ---

    @app.get(f"{base}/")
    @app.get(base)
    async def dashboard(request: Request):
        user = require_user(request)
        if isinstance(user, Response):
            return user
        return render(request, "dashboard.html", user=user, searches=searches.list_for_user(user.user_id),
                      sessions=auth.list_sessions(user.user_id))

    @app.post(f"{base}/sessions/revoke-all")
    async def revoke_all(request: Request):
        user = require_user(request, await form_data(request))
        if isinstance(user, Response):
            return user
        auth.revoke_all(user.user_id)
        response = redirect("login")
        response.delete_cookie(SESSION_COOKIE, path=base)
        return response

    @app.get(f"{base}/searches/new")
    async def new_search(request: Request):
        user = require_user(request)
        if isinstance(user, Response):
            return user
        return render(request, "search_form.html", user=user, values={"refresh_seconds": "300", "dry_run": "on",
                                                                      "telegram_timeout": "15"}, editing=False)

    def form_values(form):
        """What to put back in the form after an error: never the secrets."""
        return {k: v for k, v in form.items() if k not in SECRET_FIELDS + ("csrf",)}

    @app.post(f"{base}/searches")
    async def create(request: Request):
        form = await form_data(request)
        user = require_user(request, form)
        if isinstance(user, Response):
            return user
        try:
            settings = SearchSettings.from_form(form, today=date.today())
            secrets = SearchSecrets.from_form(form)
            search_id = searches.create(user.user_id, settings, secrets)
        except ValidationError as e:
            return render(request, "search_form.html", status=422, user=user, values=form_values(form), editing=False,
                          error=e.message, error_field=e.field)
        except LimitReached as e:
            return render(request, "search_form.html", status=409, user=user, values=form_values(form), editing=False,
                          error=str(e))
        return redirect(f"searches/{search_id}")

    @app.get(f"{base}/searches/{{search_id}}")
    async def detail(request: Request, search_id: str):
        user = require_user(request)
        if isinstance(user, Response):
            return user
        view = searches.get(user.user_id, search_id)
        if view is None:
            return bare_404()
        return render(request, "search_detail.html", user=user, s=view, discarded=searches.discarded(user.user_id, search_id),
                      proposals=searches.proposals(user.user_id, search_id))

    @app.get(f"{base}/searches/{{search_id}}/edit")
    async def edit_page(request: Request, search_id: str):
        user = require_user(request)
        if isinstance(user, Response):
            return user
        view = searches.get(user.user_id, search_id)
        if view is None:
            return bare_404()
        s = view.settings
        values = {"label": s.label, "location_mode": s.location_mode, "province": s.province, "home_comune": s.home_comune,
                  "max_km": str(s.max_km or ""), "facilities": "\n".join(f"{f['name']} | {f['province']}".rstrip(" |")
                                                                       for f in s.facilities),
                  "start_date": s.start_date, "end_date": s.end_date, "weekdays": [str(d) for d in s.weekdays],
                  "time_from": s.time_from, "time_to": s.time_to, "refresh_seconds": str(s.refresh_seconds),
                  "dry_run": "on" if s.dry_run else "", "visita_controllo": {True: "si", False: "no"}.get(s.visita_controllo, ""),
                  "telegram_timeout": str(s.telegram_timeout), "continua_dopo_prenotazione": "on" if s.continua_dopo_prenotazione else ""}
        return render(request, "search_form.html", user=user, values=values, editing=True, s=view)

    @app.post(f"{base}/searches/{{search_id}}/edit")
    async def edit(request: Request, search_id: str):
        form = await form_data(request)
        user = require_user(request, form)
        if isinstance(user, Response):
            return user
        view = searches.get(user.user_id, search_id)
        if view is None:
            return bare_404()
        changed_secrets = {k: form.get(k) for k in SECRET_FIELDS if (form.get(k) or "").strip()}
        try:
            settings = SearchSettings.from_form(form, today=date.today())
            secrets = None
            if changed_secrets:
                if not user.recently_authenticated():
                    return render(request, "confirm.html", status=403, user=user, next=f"{base}/searches/{search_id}/edit")
                old = searches.secrets_for(search_id)
                secrets = SearchSecrets.from_form({**(old.to_dict() if old else {}), **changed_secrets})
            searches.update(user.user_id, search_id, settings, secrets)
        except ValidationError as e:
            return render(request, "search_form.html", status=422, user=user, values=form_values(form), editing=True, s=view,
                          error=e.message, error_field=e.field)
        return redirect(f"searches/{search_id}")

    def action(name, fn):
        async def handler(request: Request, search_id: str):
            user = require_user(request, await form_data(request))
            if isinstance(user, Response):
                return user
            try:
                fn(user.user_id, search_id)
            except LimitReached as e:
                view = searches.get(user.user_id, search_id)
                return render(request, "search_detail.html", status=409, user=user, s=view, error=str(e),
                              discarded=searches.discarded(user.user_id, search_id),
                              proposals=searches.proposals(user.user_id, search_id))
            return redirect("" if name == "delete" else f"searches/{search_id}")
        app.post(f"{base}/searches/{{search_id}}/{name}")(handler)

    action("pause", searches.pause)
    action("resume", searches.resume)
    action("delete", searches.delete)

    @app.post(f"{base}/searches/{{search_id}}/undiscard/{{slot_id}}")
    async def undiscard(request: Request, search_id: str, slot_id: str):
        user = require_user(request, await form_data(request))
        if isinstance(user, Response):
            return user
        searches.undiscard(user.user_id, search_id, slot_id)
        return redirect(f"searches/{search_id}")

    return app
