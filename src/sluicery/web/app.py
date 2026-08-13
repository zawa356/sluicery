"""FastAPI アプリケーションのファクトリ。"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from sluicery.config import Settings
from sluicery.web.auth import (
    LOGIN_ERROR_MESSAGE,
    SESSION_COOKIE_NAME,
    AuthService,
    SessionIdentity,
)

RequestHandler = Callable[[Request], Awaitable[Response]]


def _login_page(*, error: str | None = None, username: str = "") -> str:
    error_html = f"<p>{html.escape(error)}</p>" if error else ""
    return f"""<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">
<title>ログイン - sluicery</title></head><body><main><h1>sluicery</h1>{error_html}
<form method=\"post\" action=\"/login\"><label>ユーザー名
<input name=\"username\" value=\"{html.escape(username)}\" autocomplete=\"username\"
required></label>
<label>パスワード<input type=\"password\" name=\"password\" autocomplete=\"current-password\"
required></label><button type=\"submit\">ログイン</button></form></main></body></html>"""


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

    def __init__(self, app: ASGIApp, *, auth: AuthService, settings: Settings) -> None:
        super().__init__(app)
        self.auth = auth
        self.settings = settings

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

        public = path == "/login"
        if not public and not identity.authenticated:
            response: Response = RedirectResponse("/login", status_code=303)
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


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    app = FastAPI(title="sluicery")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if settings is None or session_factory is None:
        return app

    auth = AuthService(session_factory, settings)
    app.state.auth = auth
    app.add_middleware(AuthenticationMiddleware, auth=auth, settings=settings)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> Response:
        identity: SessionIdentity = request.state.auth
        if identity.authenticated:
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(_login_page())

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request) -> Response:
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        identity: SessionIdentity = request.state.auth
        result = auth.authenticate(identity.token, username, password)
        if not result.ok or result.session is None:
            return HTMLResponse(
                _login_page(error=LOGIN_ERROR_MESSAGE, username=username),
                status_code=401,
            )
        request.state.new_session_cookie = result.session.signed_cookie
        request.state.auth = result.session.identity
        return RedirectResponse("/", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> str:
        identity: SessionIdentity = request.state.auth
        return f"<h1>sluicery</h1><p>{html.escape(identity.username or '')}</p>"

    @app.post("/logout")
    def logout(request: Request) -> Response:
        identity: SessionIdentity = request.state.auth
        auth.delete_session(identity.token)
        request.state.clear_session_cookie = True
        return RedirectResponse("/login", status_code=303)

    @app.get("/settings/password", response_class=HTMLResponse)
    def password_page() -> str:
        return """<h1>パスワード変更</h1><form method="post">
<input type="password" name="current_password" required>
<input type="password" name="new_password" minlength="12" required>
<button type="submit">変更</button></form>"""

    @app.post("/settings/password", response_class=HTMLResponse)
    async def change_password(request: Request) -> Response:
        form = await request.form()
        current_password = str(form.get("current_password", ""))
        new_password = str(form.get("new_password", ""))
        identity: SessionIdentity = request.state.auth
        if len(new_password) < 12:
            return HTMLResponse("パスワードは12文字以上にしてください", status_code=422)
        assert identity.user_id is not None
        if not auth.change_password(identity.user_id, current_password, new_password):
            return HTMLResponse("現在のパスワードが違います", status_code=400)
        request.state.clear_session_cookie = True
        return RedirectResponse("/login", status_code=303)

    return app


__all__ = ["AuthenticationMiddleware", "create_app"]
