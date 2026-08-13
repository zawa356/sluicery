"""FastAPI アプリケーションのファクトリ。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from sluicery.config import Settings
from sluicery.web.auth import (
    LOGIN_ERROR_MESSAGE,
    SESSION_COOKIE_NAME,
    AuthService,
    CsrfProtector,
    SessionIdentity,
)

RequestHandler = Callable[[Request], Awaitable[Response]]
WEB_DIR = Path(__file__).resolve().parent


def _set_session_cookie(
    response: Response,
    signed_cookie: str,
    settings: Settings,
    identity: SessionIdentity,
) -> None:
    max_age = max(0, int((identity.expires_at - datetime.now(UTC)).total_seconds()))
    response.set_cookie(
        SESSION_COOKIE_NAME,
        signed_cookie,
        max_age=max_age,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """公開パスだけを列挙し、それ以外を既定で認証必須にする。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        auth: AuthService,
        csrf: CsrfProtector,
        settings: Settings,
        templates: Jinja2Templates,
    ) -> None:
        super().__init__(app)
        self.auth = auth
        self.csrf = csrf
        self.settings = settings
        self.templates = templates

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        path = request.url.path
        if path == "/healthz" or path.startswith("/static/"):
            return await call_next(request)

        identity = self.auth.load_session(request.cookies.get(SESSION_COOKIE_NAME))
        new_cookie: str | None = None
        if identity is None:
            new_session = self.auth.create_session()
            identity = new_session.identity
            new_cookie = new_session.signed_cookie
        request.state.auth = identity
        request.state.csrf_token = self.csrf.token_for(identity.token)

        public = path == "/login"
        if not public and not identity.authenticated:
            if request.method == "GET":
                response: Response = RedirectResponse("/login", status_code=303)
            else:
                response = self.templates.TemplateResponse(
                    request,
                    "errors/403.html",
                    {
                        "auth": identity,
                        "csrf_token": request.state.csrf_token,
                        "flashes": [],
                    },
                    status_code=403,
                )
        else:
            response = await call_next(request)

        replacement = getattr(request.state, "new_session_cookie", None)
        if replacement is not None:
            _set_session_cookie(response, replacement, self.settings, request.state.auth)
        elif getattr(request.state, "clear_session_cookie", False):
            response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        elif new_cookie is not None:
            _set_session_cookie(response, new_cookie, self.settings, identity)
        return response


async def require_csrf(request: Request) -> None:
    """全APIRouteへ共通適用し、GET以外を既定で検証する。"""
    if request.method == "GET":
        return
    identity: SessionIdentity | None = getattr(request.state, "auth", None)
    if identity is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF検証に失敗しました")

    candidate = request.headers.get("X-CSRF-Token")
    content_type = request.headers.get("content-type", "").lower()
    if candidate is None and (
        content_type.startswith("application/x-www-form-urlencoded")
        or content_type.startswith("multipart/form-data")
    ):
        form = await request.form()
        form_value = form.get("csrf_token")
        candidate = str(form_value) if form_value is not None else None

    csrf: CsrfProtector = request.app.state.csrf
    if not csrf.verify(identity.token, candidate):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF検証に失敗しました")


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    app = FastAPI(title="sluicery", dependencies=[Depends(require_csrf)])
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if settings is None or session_factory is None:
        return app

    auth = AuthService(session_factory, settings)
    csrf = CsrfProtector(settings.SECRET_KEY)
    app.state.auth = auth
    app.state.csrf = csrf
    app.state.templates = templates
    app.add_middleware(
        AuthenticationMiddleware,
        auth=auth,
        csrf=csrf,
        settings=settings,
        templates=templates,
    )

    def context(request: Request, **values: Any) -> dict[str, Any]:
        identity: SessionIdentity = request.state.auth
        return {
            "auth": identity,
            "csrf_token": request.state.csrf_token,
            "flashes": auth.pop_flashes(identity),
            **values,
        }

    def error_context(request: Request, **values: Any) -> dict[str, Any]:
        identity: SessionIdentity | None = getattr(request.state, "auth", None)
        return {
            "auth": identity,
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "flashes": [],
            **values,
        }

    @app.get("/login")
    def login_page(request: Request) -> Response:
        identity: SessionIdentity = request.state.auth
        if identity.authenticated:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", context(request))

    @app.post("/login")
    async def login(request: Request) -> Response:
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        identity: SessionIdentity = request.state.auth
        result = auth.authenticate(identity.token, username, password)
        if not result.ok or result.session is None:
            return templates.TemplateResponse(
                request,
                "login.html",
                context(request, error=LOGIN_ERROR_MESSAGE, username=username),
                status_code=401,
            )
        request.state.new_session_cookie = result.session.signed_cookie
        request.state.auth = result.session.identity
        return RedirectResponse("/", status_code=303)

    @app.get("/")
    def home(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            context(request, active_nav="dashboard"),
        )

    @app.get("/playlists")
    def playlists(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "placeholder.html",
            context(request, active_nav="playlists", title="Playlist", phase="Part B"),
        )

    @app.get("/profiles")
    def profiles(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "placeholder.html",
            context(request, active_nav="profiles", title="Profile", phase="Part B"),
        )

    @app.get("/storages")
    def storages(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "placeholder.html",
            context(request, active_nav="storages", title="Storage", phase="Part B"),
        )

    @app.get("/runs")
    def runs(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "placeholder.html",
            context(request, active_nav="runs", title="Run 履歴", phase="Part C"),
        )

    @app.get("/reports")
    def reports(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "placeholder.html",
            context(request, active_nav="reports", title="レポート", phase="Phase 13"),
        )

    @app.get("/settings")
    def settings_page(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "placeholder.html",
            context(request, active_nav="settings", title="設定", phase="Part B"),
        )

    @app.post("/logout")
    def logout(request: Request) -> Response:
        identity: SessionIdentity = request.state.auth
        auth.delete_session(identity.token)
        request.state.clear_session_cookie = True
        return RedirectResponse("/login", status_code=303)

    @app.get("/settings/password")
    def password_page(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "password.html",
            context(request, active_nav="settings"),
        )

    @app.post("/settings/password")
    async def change_password(request: Request) -> Response:
        form = await request.form()
        current_password = str(form.get("current_password", ""))
        new_password = str(form.get("new_password", ""))
        identity: SessionIdentity = request.state.auth
        if len(new_password) < 12:
            return templates.TemplateResponse(
                request,
                "password.html",
                context(
                    request,
                    active_nav="settings",
                    error="新しいパスワードは12文字以上にしてください",
                ),
                status_code=422,
            )
        assert identity.user_id is not None
        if not auth.change_password(identity.user_id, current_password, new_password):
            return templates.TemplateResponse(
                request,
                "password.html",
                context(request, active_nav="settings", error="現在のパスワードが違います"),
                status_code=400,
            )
        anonymous = auth.create_session()
        auth.add_flash(
            anonymous.identity,
            "success",
            "パスワードを変更しました。再ログインしてください",
        )
        request.state.auth = anonymous.identity
        request.state.csrf_token = csrf.token_for(anonymous.token)
        request.state.new_session_cookie = anonymous.signed_cookie
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(403)
    async def forbidden(request: Request, exc: HTTPException) -> Response:
        return templates.TemplateResponse(
            request,
            "errors/403.html",
            error_context(request),
            status_code=403,
        )

    @app.exception_handler(404)
    async def not_found(request: Request, exc: HTTPException) -> Response:
        return templates.TemplateResponse(
            request,
            "errors/404.html",
            error_context(request),
            status_code=404,
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> Response:
        return templates.TemplateResponse(
            request,
            "errors/500.html",
            error_context(request),
            status_code=500,
        )

    return app


__all__ = ["AuthenticationMiddleware", "create_app", "require_csrf"]
