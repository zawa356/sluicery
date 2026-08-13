"""FastAPI アプリケーションのファクトリ。"""

from __future__ import annotations

import json
import math
import shlex
import shutil
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import UploadFile
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from sluicery.config import Settings
from sluicery.core import settings as core_settings
from sluicery.core.cookies import (
    MAX_COOKIE_BYTES,
    CookieConfigurationError,
    clear_playlist_cookie,
    playlist_cookie_configured,
    save_playlist_cookie,
    set_playlist_cookie_enabled,
)
from sluicery.core.naming import NamingValidationError, sanitize_component
from sluicery.core.options import (
    OptionValidationError,
    build_download_args,
    guard_freeform,
    validate_source_url,
)
from sluicery.core.settings import OperationalSettings
from sluicery.core.sync import enqueue_discover_run, execute_download_run
from sluicery.core.target_state import sync_target_after_task, transition_target
from sluicery.db.models import (
    Item,
    ItemMembership,
    LayoutStrategy,
    Playlist,
    PlaylistKindHint,
    PlaylistProfile,
    Profile,
    ProfileKind,
    Run,
    RunStatus,
    Storage,
    StorageKind,
    Target,
    TargetStatus,
    Task,
    TaskStatus,
)
from sluicery.db.repositories.playlist import PlaylistRepository
from sluicery.db.repositories.playlist_profile import PlaylistProfileRepository
from sluicery.db.repositories.profile import ProfileRepository
from sluicery.db.repositories.run import RunRepository
from sluicery.db.repositories.storage import StorageRepository
from sluicery.db.repositories.task import TaskRepository
from sluicery.db.repositories.ytdlp_release import YtdlpReleaseRepository
from sluicery.downloader.version import get_status, ytdlp_root
from sluicery.downloader.ytdlp import mask_command_line
from sluicery.layout import LayoutContext, LayoutValidationError, resolve_layout
from sluicery.runner.base import mask_log_text
from sluicery.storage import create_storage_adapter
from sluicery.storage.base import StoragePathError, validate_relative_path
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

    def filesize(value: int | None) -> str:
        if value is None:
            return "—"
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        size = float(value)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TiB"

    templates.env.filters["filesize"] = filesize
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
        with session_factory() as db:
            recent_runs = RunRepository(db).list_recent(10)
            playlist_names = {row.id: row.name for row in db.scalars(select(Playlist)).all()}
            target_counts = {
                status.value: count
                for status, count in db.execute(
                    select(Target.status, func.count()).group_by(Target.status)
                )
            }
            delisted_count = (
                db.scalar(
                    select(func.count())
                    .select_from(Item)
                    .where(Item.membership == ItemMembership.DELISTED)
                )
                or 0
            )
            storages = list(db.scalars(select(Storage).order_by(Storage.id)))
            active_release = YtdlpReleaseRepository(db).get_active()
        assert settings.STAGING_DIR is not None
        usage = shutil.disk_usage(settings.STAGING_DIR)
        ytdlp_status = get_status(ytdlp_root(settings.DATA_DIR))
        run_rows = [
            {
                "id": run.id,
                "playlist": (
                    playlist_names.get(run.playlist_id, "—") if run.playlist_id is not None else "—"
                ),
                "kind": run.kind,
                "status": run.status.value,
                "started_at": run.started_at,
            }
            for run in recent_runs
        ]
        storage_rows = [
            {
                "id": storage.id,
                "name": storage.name,
                "kind": storage.kind.value,
                "enabled": storage.enabled,
                "reachable": (
                    storage.last_check_result_json.get("ok")
                    if isinstance(storage.last_check_result_json, dict)
                    else None
                ),
                "last_check_at": storage.last_check_at,
            }
            for storage in storages
        ]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            context(
                request,
                active_nav="dashboard",
                recent_runs=run_rows,
                ytdlp_status=ytdlp_status.status.value,
                ytdlp_version=ytdlp_status.current_version,
                ytdlp_updated_at=(active_release.installed_at if active_release else None),
                staging_used=usage.used,
                staging_total=usage.total,
                staging_pct=round(usage.used / usage.total * 100, 1) if usage.total else 0,
                storage_rows=storage_rows,
                failed_count=target_counts.get(TargetStatus.FAILED.value, 0),
                missing_count=target_counts.get(TargetStatus.MISSING.value, 0),
                delisted_count=delisted_count,
            ),
        )

    @app.get("/playlists")
    def playlists(request: Request) -> Response:
        with session_factory() as db:
            rows = []
            for playlist in db.scalars(select(Playlist).order_by(Playlist.id)):
                item_count = (
                    db.scalar(
                        select(func.count())
                        .select_from(Item)
                        .where(Item.playlist_id == playlist.id)
                    )
                    or 0
                )
                rows.append({"playlist": playlist, "item_count": item_count})
        return templates.TemplateResponse(
            request,
            "playlists/list.html",
            context(request, active_nav="playlists", rows=rows),
        )

    def playlist_form_values(form: Any) -> dict[str, Any]:
        name = str(form.get("name", "")).strip()
        if not name:
            raise ValueError("名前を入力してください")
        try:
            folder_name = sanitize_component(str(form.get("folder_name", "")))
            url = validate_source_url(str(form.get("url", "")))
            kind_hint = PlaylistKindHint(str(form.get("kind_hint", "video")))
            ytdlp_args = str(form.get("ytdlp_args", "")).strip() or None
            guard_freeform(ytdlp_args, source_label="Playlist")
        except (NamingValidationError, OptionValidationError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        return {
            "name": name,
            "folder_name": folder_name,
            "url": url,
            "kind_hint": kind_hint,
            "ytdlp_args": ytdlp_args,
            "enabled": form.get("enabled") == "yes",
            "paused": form.get("paused") == "yes",
        }

    def playlist_editor_response(
        request: Request,
        *,
        playlist: Playlist | None = None,
        values: dict[str, Any] | None = None,
        error: str | None = None,
        status_code: int = 200,
    ) -> Response:
        return templates.TemplateResponse(
            request,
            "playlists/form.html",
            context(
                request,
                active_nav="playlists",
                playlist=playlist,
                values=values or {},
                error=error,
                kinds=[kind.value for kind in PlaylistKindHint],
            ),
            status_code=status_code,
        )

    @app.get("/playlists/new")
    def playlist_new(request: Request) -> Response:
        return playlist_editor_response(request)

    @app.post("/playlists/new")
    async def playlist_create(request: Request) -> Response:
        form = await request.form()
        try:
            values = playlist_form_values(form)
        except ValueError as exc:
            return playlist_editor_response(
                request,
                values=dict(form),
                error=str(exc),
                status_code=422,
            )
        with session_factory() as db:
            playlist = PlaylistRepository(db).create(**values, dedup_hardlink=False)
        auth.add_flash(request.state.auth, "success", "Playlistを作成しました")
        return RedirectResponse(f"/playlists/{playlist.id}", status_code=303)

    @app.get("/playlists/{playlist_id}/edit")
    def playlist_edit(request: Request, playlist_id: int) -> Response:
        with session_factory() as db:
            playlist = db.get(Playlist, playlist_id)
            if playlist is None:
                raise HTTPException(status_code=404)
            return playlist_editor_response(request, playlist=playlist)

    @app.post("/playlists/{playlist_id}/edit")
    async def playlist_update(request: Request, playlist_id: int) -> Response:
        form = await request.form()
        with session_factory() as db:
            playlist = db.get(Playlist, playlist_id)
            if playlist is None:
                raise HTTPException(status_code=404)
            try:
                values = playlist_form_values(form)
            except ValueError as exc:
                return playlist_editor_response(
                    request,
                    playlist=playlist,
                    values=dict(form),
                    error=str(exc),
                    status_code=422,
                )
            PlaylistRepository(db).update(playlist, **values)
        auth.add_flash(request.state.auth, "success", "Playlistを更新しました")
        return RedirectResponse(f"/playlists/{playlist_id}", status_code=303)

    @app.get("/playlists/{playlist_id}")
    def playlist_detail(
        request: Request,
        playlist_id: int,
        page: int = 1,
        status_filter: str | None = None,
        q: str = "",
    ) -> Response:
        page = max(1, page)
        per_page = 50
        with session_factory() as db:
            playlist = db.get(Playlist, playlist_id)
            if playlist is None:
                raise HTTPException(status_code=404)
            stmt = (
                select(Target, Item, Profile)
                .join(Item, Target.item_id == Item.id)
                .join(PlaylistProfile, Target.playlist_profile_id == PlaylistProfile.id)
                .join(Profile, PlaylistProfile.profile_id == Profile.id)
                .where(Item.playlist_id == playlist_id)
            )
            if status_filter:
                try:
                    stmt = stmt.where(Target.status == TargetStatus(status_filter))
                except ValueError:
                    raise HTTPException(status_code=422, detail="不正な状態です") from None
            if q.strip():
                pattern = f"%{q.strip()}%"
                stmt = stmt.where(Item.title.ilike(pattern) | Item.source_id.ilike(pattern))
            total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
            target_rows = [
                tuple(row)
                for row in db.execute(
                    stmt.order_by(Item.playlist_index.is_(None), Item.playlist_index, Target.id)
                    .offset((page - 1) * per_page)
                    .limit(per_page)
                )
            ]
            assignments = list(
                db.execute(
                    select(PlaylistProfile, Profile, Storage)
                    .join(Profile, PlaylistProfile.profile_id == Profile.id)
                    .join(Storage, PlaylistProfile.storage_id == Storage.id)
                    .where(PlaylistProfile.playlist_id == playlist_id)
                    .order_by(PlaylistProfile.sort_order, PlaylistProfile.id)
                )
            )
            profiles = list(db.scalars(select(Profile).order_by(Profile.name)))
            storages = list(db.scalars(select(Storage).order_by(Storage.name)))
        return templates.TemplateResponse(
            request,
            "playlists/detail.html",
            context(
                request,
                active_nav="playlists",
                playlist=playlist,
                target_rows=target_rows,
                statuses=[status.value for status in TargetStatus],
                status_filter=status_filter or "",
                q=q,
                page=page,
                pages=max(1, (total + per_page - 1) // per_page),
                total=total,
                assignments=assignments,
                profiles=profiles,
                storages=storages,
            ),
        )

    @app.post("/playlists/{playlist_id}/run")
    async def playlist_run(request: Request, playlist_id: int) -> Response:
        form = await request.form()
        kind = str(form.get("kind", "discover"))
        try:
            with session_factory() as db:
                if kind == "discover":
                    run, _task = enqueue_discover_run(db, playlist_id)
                elif kind == "download":
                    run = execute_download_run(db, playlist_id)
                else:
                    raise ValueError("実行種別が不正です")
        except (LookupError, ValueError) as exc:
            auth.add_flash(request.state.auth, "error", str(exc))
        else:
            auth.add_flash(request.state.auth, "success", f"Run {run.id}を開始しました")
        return RedirectResponse(f"/playlists/{playlist_id}", status_code=303)

    @app.post("/playlists/{playlist_id}/assignments")
    async def playlist_assign(request: Request, playlist_id: int) -> Response:
        form = await request.form()
        try:
            profile_id = int(str(form.get("profile_id", "")))
            storage_id = int(str(form.get("storage_id", "")))
            subpath = str(form.get("subpath", "{playlist.folder_name}"))
        except ValueError:
            raise HTTPException(status_code=422, detail="割当値が不正です") from None
        with session_factory() as db:
            if db.get(Playlist, playlist_id) is None:
                raise HTTPException(status_code=404)
            if db.get(Profile, profile_id) is None or db.get(Storage, storage_id) is None:
                raise HTTPException(status_code=422, detail="ProfileまたはStorageが不正です")
            existing = db.scalar(
                select(PlaylistProfile).where(
                    PlaylistProfile.playlist_id == playlist_id,
                    PlaylistProfile.profile_id == profile_id,
                )
            )
            if existing is not None:
                auth.add_flash(request.state.auth, "error", "Profileは割当済みです")
            else:
                PlaylistProfileRepository(db).create(
                    playlist_id=playlist_id,
                    profile_id=profile_id,
                    storage_id=storage_id,
                    subpath=subpath,
                    enabled=True,
                    sort_order=0,
                )
                auth.add_flash(request.state.auth, "success", "Profileを割り当てました")
        return RedirectResponse(f"/playlists/{playlist_id}", status_code=303)

    @app.post("/playlists/{playlist_id}/assignments/{assignment_id}/delete")
    def playlist_detach(request: Request, playlist_id: int, assignment_id: int) -> Response:
        with session_factory() as db:
            assignment = db.get(PlaylistProfile, assignment_id)
            if assignment is None or assignment.playlist_id != playlist_id:
                raise HTTPException(status_code=404)
            count = (
                db.scalar(
                    select(func.count())
                    .select_from(Target)
                    .where(Target.playlist_profile_id == assignment_id)
                )
                or 0
            )
            if count:
                auth.add_flash(request.state.auth, "error", "Targetが存在する割当は解除できません")
            else:
                PlaylistProfileRepository(db).delete(assignment)
                auth.add_flash(request.state.auth, "success", "割当を解除しました")
        return RedirectResponse(f"/playlists/{playlist_id}", status_code=303)

    @app.post("/playlists/{playlist_id}/targets/{target_id}/action")
    async def playlist_target_action(
        request: Request, playlist_id: int, target_id: int
    ) -> Response:
        form = await request.form()
        action = str(form.get("action", ""))
        with session_factory() as db:
            target = db.get(Target, target_id)
            item = db.get(Item, target.item_id) if target else None
            if target is None or item is None or item.playlist_id != playlist_id:
                raise HTTPException(status_code=404)
            try:
                if action == "ignore":
                    transition_target(db, target_id, TargetStatus.IGNORED)
                elif action == "retry" and target.status in {
                    TargetStatus.FAILED,
                    TargetStatus.BLOCKED,
                }:
                    transition_target(db, target_id, TargetStatus.PENDING)
                elif action == "retry" and target.status == TargetStatus.IGNORED:
                    target.status = TargetStatus.PENDING
                    target.last_error = None
                    db.commit()
                else:
                    raise ValueError("この状態では操作できません")
            except ValueError as exc:
                auth.add_flash(request.state.auth, "error", str(exc))
            else:
                auth.add_flash(request.state.auth, "success", "Target状態を更新しました")
        return RedirectResponse(f"/playlists/{playlist_id}", status_code=303)

    @app.post("/playlists/{playlist_id}/delete")
    async def playlist_delete(request: Request, playlist_id: int) -> Response:
        form = await request.form()
        mode = str(form.get("mode", ""))
        with session_factory() as db:
            playlist = db.get(Playlist, playlist_id)
            if playlist is None:
                raise HTTPException(status_code=404)
            if mode == "keep_items":
                PlaylistRepository(db).update(playlist, enabled=False, paused=True)
                message = "Playlistを無効化・一時停止しました。Itemとメディアは保持されます"
            elif mode == "delete_items":
                run_count = (
                    db.scalar(
                        select(func.count()).select_from(Run).where(Run.playlist_id == playlist_id)
                    )
                    or 0
                )
                if run_count:
                    auth.add_flash(
                        request.state.auth,
                        "error",
                        "Run履歴が参照するPlaylistは削除できません",
                    )
                    return RedirectResponse(f"/playlists/{playlist_id}", status_code=303)
                db.execute(delete(Item).where(Item.playlist_id == playlist_id))
                db.execute(
                    delete(PlaylistProfile).where(PlaylistProfile.playlist_id == playlist_id)
                )
                db.delete(playlist)
                db.commit()
                message = "PlaylistとItem関連DBレコードを削除しました。メディアは削除していません"
            else:
                raise HTTPException(status_code=422, detail="削除方法を選択してください")
        auth.add_flash(request.state.auth, "success", message)
        return RedirectResponse("/playlists", status_code=303)

    @app.get("/profiles")
    def profiles(request: Request) -> Response:
        with session_factory() as db:
            rows = []
            for profile in db.scalars(select(Profile).order_by(Profile.id)):
                refs = (
                    db.scalar(
                        select(func.count())
                        .select_from(PlaylistProfile)
                        .where(PlaylistProfile.profile_id == profile.id)
                    )
                    or 0
                )
                rows.append({"profile": profile, "refs": refs})
        return templates.TemplateResponse(
            request,
            "profiles/list.html",
            context(request, active_nav="profiles", rows=rows),
        )

    tristate_fields = (
        "audio_extract",
        "embed_metadata",
        "embed_thumbnail",
        "embed_chapters",
        "subtitle_auto",
        "subtitle_embed",
    )

    def parse_tristate(value: object) -> bool | None:
        if value == "inherit":
            return None
        if value == "true":
            return True
        if value == "false":
            return False
        raise ValueError("三状態の値が不正です")

    def profile_form_values(form: Any, current: Profile | None = None) -> dict[str, Any]:
        name = str(form.get("name", "")).strip()
        if not name:
            raise ValueError("名前を入力してください")
        try:
            kind = ProfileKind(str(form.get("kind", "video")))
            strategy = LayoutStrategy(str(form.get("layout_strategy", "flat")))
            ytdlp_args = str(form.get("ytdlp_args", "")).strip() or None
            expert_mode = form.get("expert_mode") == "yes"
            allow_exec = form.get("allow_exec") == "yes"
            guard_freeform(
                ytdlp_args,
                source_label=f"Profile {name}",
                expert_mode=expert_mode,
                env_allow_exec=settings.ALLOW_EXEC,
                profile_allow_exec=allow_exec,
            )
            output_template = str(form.get("output_template", "")).strip() or None
            resolve_layout(
                strategy.value,
                LayoutContext(
                    playlist_name="preview",
                    playlist_folder_name="preview",
                    profile_name=name,
                    profile_kind=kind.value,
                    subpath="preview",
                    custom_output_template=output_template,
                ),
            )
            concurrent_raw = str(form.get("concurrent_fragments", "")).strip()
            concurrent = int(concurrent_raw) if concurrent_raw else None
            if concurrent is not None and concurrent < 1:
                raise ValueError("concurrent_fragmentsは1以上にしてください")
            tristates = {
                field: parse_tristate(form.get(field, "inherit")) for field in tristate_fields
            }
        except (OptionValidationError, LayoutValidationError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        values: dict[str, Any] = {
            "name": name,
            "description": str(form.get("description", "")).strip() or None,
            "kind": kind,
            "layout_strategy": strategy,
            "output_template": output_template,
            "ytdlp_args": ytdlp_args,
            "format_selector": str(form.get("format_selector", "")).strip() or None,
            "container": str(form.get("container", "")).strip() or None,
            "audio_format": str(form.get("audio_format", "")).strip() or None,
            "audio_quality": str(form.get("audio_quality", "")).strip() or None,
            "subtitle_langs": str(form.get("subtitle_langs", "")).strip() or None,
            "concurrent_fragments": concurrent,
            "expert_mode": expert_mode,
            "allow_exec": allow_exec,
            **tristates,
        }
        if current is None:
            values["postprocess_chain_json"] = []
        return values

    def profile_editor_response(
        request: Request,
        *,
        profile: Profile | None = None,
        values: dict[str, Any] | None = None,
        error: str | None = None,
        status_code: int = 200,
    ) -> Response:
        refs: list[tuple[Playlist, PlaylistProfile]] = []
        preview: dict[str, Any] | None = None
        assert settings.STAGING_DIR is not None
        if profile is not None:
            with session_factory() as db:
                refs = [
                    tuple(row)
                    for row in db.execute(
                        select(Playlist, PlaylistProfile)
                        .join(PlaylistProfile, Playlist.id == PlaylistProfile.playlist_id)
                        .where(PlaylistProfile.profile_id == profile.id)
                        .order_by(Playlist.name)
                    )
                ]
                if refs:
                    playlist, assignment = refs[0]
                    persisted = db.get(Profile, profile.id)
                    assert persisted is not None
                    built = build_download_args(
                        None,
                        source_url=playlist.url,
                        session=db,
                        staging_dir=settings.STAGING_DIR,
                        work_id="<work-id>",
                        playlist=playlist,
                        profile=persisted,
                        playlist_profile=assignment,
                        env_allow_exec=settings.ALLOW_EXEC,
                    )
                    preview = {
                        "playlist": playlist.name,
                        "command": shlex.join(["yt-dlp", *mask_command_line(built.args)]),
                        "origins": [
                            {
                                "arguments": shlex.join(mask_command_line(origin.arguments)),
                                "layer": origin.layer,
                                "source": origin.source,
                                "field": origin.field,
                            }
                            for origin in built.origins
                        ],
                        "warnings": built.warnings,
                    }
        return templates.TemplateResponse(
            request,
            "profiles/form.html",
            context(
                request,
                active_nav="profiles",
                profile=profile,
                values=values or {},
                error=error,
                kinds=[kind.value for kind in ProfileKind],
                strategies=[strategy.value for strategy in LayoutStrategy],
                tristate_fields=tristate_fields,
                refs=refs,
                preview=preview,
                env_allow_exec=settings.ALLOW_EXEC,
            ),
            status_code=status_code,
        )

    @app.get("/profiles/new")
    def profile_new(request: Request) -> Response:
        return profile_editor_response(request)

    @app.post("/profiles/new")
    async def profile_create(request: Request) -> Response:
        form = await request.form()
        try:
            values = profile_form_values(form)
        except ValueError as exc:
            return profile_editor_response(
                request, values=dict(form), error=str(exc), status_code=422
            )
        with session_factory() as db:
            profile = ProfileRepository(db).create(**values)
        auth.add_flash(request.state.auth, "success", "Profileを作成しました")
        return RedirectResponse(f"/profiles/{profile.id}/edit", status_code=303)

    @app.get("/profiles/{profile_id}/edit")
    def profile_edit(request: Request, profile_id: int) -> Response:
        with session_factory() as db:
            profile = db.get(Profile, profile_id)
            if profile is None:
                raise HTTPException(status_code=404)
            return profile_editor_response(request, profile=profile)

    @app.post("/profiles/{profile_id}/edit")
    async def profile_update(request: Request, profile_id: int) -> Response:
        form = await request.form()
        with session_factory() as db:
            profile = db.get(Profile, profile_id)
            if profile is None:
                raise HTTPException(status_code=404)
            try:
                values = profile_form_values(form, profile)
            except ValueError as exc:
                return profile_editor_response(
                    request,
                    profile=profile,
                    values=dict(form),
                    error=str(exc),
                    status_code=422,
                )
            ProfileRepository(db).update(profile, **values)
        auth.add_flash(request.state.auth, "success", "Profileを更新しました")
        return RedirectResponse(f"/profiles/{profile_id}/edit", status_code=303)

    @app.post("/profiles/{profile_id}/delete")
    def profile_delete(request: Request, profile_id: int) -> Response:
        with session_factory() as db:
            profile = db.get(Profile, profile_id)
            if profile is None:
                raise HTTPException(status_code=404)
            refs = (
                db.scalar(
                    select(func.count())
                    .select_from(PlaylistProfile)
                    .where(PlaylistProfile.profile_id == profile_id)
                )
                or 0
            )
            if refs:
                auth.add_flash(
                    request.state.auth,
                    "error",
                    "Playlistから参照されているProfileは削除できません",
                )
                return RedirectResponse(f"/profiles/{profile_id}/edit", status_code=303)
            ProfileRepository(db).delete(profile)
        auth.add_flash(request.state.auth, "success", "Profileを削除しました")
        return RedirectResponse("/profiles", status_code=303)

    @app.get("/storages")
    def storages(request: Request) -> Response:
        with session_factory() as db:
            statement = (
                select(Storage, func.count(PlaylistProfile.id))
                .outerjoin(PlaylistProfile, PlaylistProfile.storage_id == Storage.id)
                .group_by(Storage.id)
                .order_by(Storage.id)
            )
            rows = [
                {
                    "storage": storage,
                    "refs": refs,
                    "credentials_configured": storage.credentials_encrypted is not None,
                }
                for storage, refs in db.execute(statement)
            ]
        return templates.TemplateResponse(
            request,
            "storages/list.html",
            context(request, active_nav="storages", rows=rows),
        )

    def normalize_local_path(value: str) -> str:
        path = Path(value)
        if path.is_absolute():
            resolved = path.resolve(strict=False)
            if not resolved.is_relative_to(Path("/mnt/media")):
                raise ValueError("local Storageの絶対pathは/mnt/media配下にしてください")
            return str(resolved)
        try:
            return validate_relative_path(value, allow_empty=True)
        except StoragePathError as exc:
            raise ValueError(str(exc)) from exc

    def storage_form_values(
        form: Any, current: Storage | None = None
    ) -> tuple[dict[str, Any], dict[str, str] | None]:
        name = str(form.get("name", "")).strip()
        if not name:
            raise ValueError("名前を入力してください")
        try:
            kind = StorageKind(str(form.get("kind", "local")))
        except ValueError:
            raise ValueError("Storage kindが不正です") from None
        if kind == StorageKind.MOUNT:
            raise ValueError("mount StorageはPhase 19で実装予定です")
        values: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "enabled": form.get("enabled") == "yes",
        }
        credentials: dict[str, str] | None = None
        if kind == StorageKind.LOCAL:
            values["config_json"] = {"path": normalize_local_path(str(form.get("path", "")))}
            values["credentials_encrypted"] = None
        else:
            protocol = str(form.get("protocol", "smb")).strip()
            host = str(form.get("host", "")).strip()
            share = str(form.get("share", "")).strip()
            user = str(form.get("user", "")).strip()
            password = str(form.get("password", ""))
            domain = str(form.get("domain", "")).strip()
            if protocol != "smb":
                raise ValueError("検証済みのremote protocolはSMBだけです")
            if not host or not share or not user:
                raise ValueError("remote Storageにはhost / share / userが必要です")
            if "/" in share or "\\" in share or share in {".", ".."}:
                raise ValueError("shareにパス区切りは使用できません")
            try:
                port = int(str(form.get("port", "445")))
                if not 1 <= port <= 65535:
                    raise ValueError
                remote_path = validate_relative_path(
                    str(form.get("path_remote", "")), allow_empty=True
                )
            except (ValueError, StoragePathError):
                raise ValueError("portまたはremote pathが不正です") from None
            values["config_json"] = {
                "protocol": "smb",
                "host": host,
                "share": share,
                "path": remote_path,
                "port": port,
            }
            if password:
                credentials = {"user": user, "password": password, "domain": domain}
            elif current is None or not isinstance(current.credentials_encrypted, dict):
                raise ValueError("新規remote Storageにはpasswordが必要です")
            else:
                credentials = dict(current.credentials_encrypted)
                credentials.update({"user": user, "domain": domain})
            values["credentials_encrypted"] = credentials
        return values, credentials

    def storage_editor_response(
        request: Request,
        *,
        storage: Storage | None = None,
        values: dict[str, Any] | None = None,
        error: str | None = None,
        status_code: int = 200,
    ) -> Response:
        config = storage.config_json if storage and isinstance(storage.config_json, dict) else {}
        credentials = (
            storage.credentials_encrypted
            if storage and isinstance(storage.credentials_encrypted, dict)
            else {}
        )
        return templates.TemplateResponse(
            request,
            "storages/form.html",
            context(
                request,
                active_nav="storages",
                storage=storage,
                values=values or {},
                config=config,
                credential_user=credentials.get("user", ""),
                credential_domain=credentials.get("domain", ""),
                credentials_configured=bool(credentials),
                error=error,
            ),
            status_code=status_code,
        )

    @app.get("/storages/new")
    def storage_new(request: Request) -> Response:
        return storage_editor_response(request)

    @app.post("/storages/new")
    async def storage_create(request: Request) -> Response:
        form = await request.form()
        try:
            values, _credentials = storage_form_values(form)
        except ValueError as exc:
            safe_values = {key: value for key, value in form.items() if key != "password"}
            return storage_editor_response(
                request, values=safe_values, error=str(exc), status_code=422
            )
        with session_factory() as db:
            storage = StorageRepository(db).create(**values)
        auth.add_flash(request.state.auth, "success", "Storageを作成しました")
        return RedirectResponse(f"/storages/{storage.id}/edit", status_code=303)

    @app.get("/storages/{storage_id}/edit")
    def storage_edit(request: Request, storage_id: int) -> Response:
        with session_factory() as db:
            storage = db.get(Storage, storage_id)
            if storage is None:
                raise HTTPException(status_code=404)
            return storage_editor_response(request, storage=storage)

    @app.post("/storages/{storage_id}/edit")
    async def storage_update(request: Request, storage_id: int) -> Response:
        form = await request.form()
        with session_factory() as db:
            storage = db.get(Storage, storage_id)
            if storage is None:
                raise HTTPException(status_code=404)
            try:
                values, _credentials = storage_form_values(form, storage)
            except ValueError as exc:
                safe_values = {key: value for key, value in form.items() if key != "password"}
                return storage_editor_response(
                    request,
                    storage=storage,
                    values=safe_values,
                    error=str(exc),
                    status_code=422,
                )
            StorageRepository(db).update(storage, **values)
        auth.add_flash(request.state.auth, "success", "Storageを更新しました")
        return RedirectResponse(f"/storages/{storage_id}/edit", status_code=303)

    @app.post("/storages/{storage_id}/test")
    def storage_test(request: Request, storage_id: int) -> Response:
        with session_factory() as db:
            storage = db.get(Storage, storage_id)
            if storage is None:
                raise HTTPException(status_code=404)
            try:
                adapter = create_storage_adapter(storage, OperationalSettings(db))
                result = adapter.test_connection()
                free_bytes = adapter.free_space()
                result_json = {
                    "ok": result.ok,
                    "stages": [
                        {
                            "stage": stage.stage.value,
                            "status": stage.status.value,
                            "message": stage.message,
                            "classification": stage.classification.value,
                            "reason_code": stage.reason_code,
                        }
                        for stage in result.stages
                    ],
                    "cleanup_warning": result.cleanup_warning,
                    "free_bytes": free_bytes,
                }
            except Exception as exc:  # noqa: BLE001 - UIへ秘密を含まない型名だけ返す
                result_json = {
                    "ok": False,
                    "stages": [],
                    "error": f"接続テストを実行できません: {type(exc).__name__}",
                    "free_bytes": None,
                }
            storage.last_check_at = datetime.now(UTC)
            storage.last_check_result_json = result_json
            db.commit()
        auth.add_flash(
            request.state.auth,
            "success" if result_json["ok"] else "error",
            "接続テストが完了しました",
        )
        return RedirectResponse(f"/storages/{storage_id}/edit", status_code=303)

    @app.post("/storages/{storage_id}/delete")
    def storage_delete(request: Request, storage_id: int) -> Response:
        with session_factory() as db:
            storage = db.get(Storage, storage_id)
            if storage is None:
                raise HTTPException(status_code=404)
            refs = (
                db.scalar(
                    select(func.count())
                    .select_from(PlaylistProfile)
                    .where(PlaylistProfile.storage_id == storage_id)
                )
                or 0
            )
            if refs:
                auth.add_flash(request.state.auth, "error", "割当中のStorageは削除できません")
                return RedirectResponse(f"/storages/{storage_id}/edit", status_code=303)
            StorageRepository(db).delete(storage)
        auth.add_flash(request.state.auth, "success", "Storageレコードを削除しました")
        return RedirectResponse("/storages", status_code=303)

    @app.get("/runs")
    def runs(request: Request, page: int = 1, status_filter: str = "") -> Response:
        page = max(1, page)
        per_page = 50
        statement = (
            select(Run, Playlist.name)
            .outerjoin(Playlist, Playlist.id == Run.playlist_id)
            .order_by(Run.started_at.desc(), Run.id.desc())
        )
        if status_filter:
            try:
                selected_status = RunStatus(status_filter)
            except ValueError:
                raise HTTPException(status_code=422, detail="不正なRun状態です") from None
            statement = statement.where(Run.status == selected_status)
        with session_factory() as db:
            total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
            run_rows = list(db.execute(statement.offset((page - 1) * per_page).limit(per_page)))
            run_ids = [run.id for run, _playlist_name in run_rows]
            task_counts: dict[int, dict[str, int]] = {run_id: {} for run_id in run_ids}
            if run_ids:
                for run_id, task_status, count in db.execute(
                    select(Task.run_id, Task.status, func.count(Task.id))
                    .where(Task.run_id.in_(run_ids))
                    .group_by(Task.run_id, Task.status)
                ):
                    assert run_id is not None
                    task_counts[run_id][task_status.value] = count
            rows = [
                {
                    "run": run,
                    "playlist_name": playlist_name or "—",
                    "task_counts": task_counts[run.id],
                }
                for run, playlist_name in run_rows
            ]
        return templates.TemplateResponse(
            request,
            "runs/list.html",
            context(
                request,
                active_nav="runs",
                rows=rows,
                page=page,
                pages=max(1, (total + per_page - 1) // per_page),
                total=total,
                status_filter=status_filter,
                statuses=[value.value for value in RunStatus],
                now=datetime.now(UTC),
            ),
        )

    @app.get("/runs/{run_id}")
    def run_detail(
        request: Request, run_id: int, page: int = 1, status_filter: str = ""
    ) -> Response:
        page = max(1, page)
        per_page = 50
        with session_factory() as db:
            row = db.execute(
                select(Run, Playlist.name)
                .outerjoin(Playlist, Playlist.id == Run.playlist_id)
                .where(Run.id == run_id)
            ).one_or_none()
            if row is None:
                raise HTTPException(status_code=404)
            run, playlist_name = row
            statement = select(Task).where(Task.run_id == run_id).order_by(Task.id)
            if status_filter:
                try:
                    selected_status = TaskStatus(status_filter)
                except ValueError:
                    raise HTTPException(status_code=422, detail="不正なTask状態です") from None
                statement = statement.where(Task.status == selected_status)
            total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
            tasks = list(db.scalars(statement.offset((page - 1) * per_page).limit(per_page)))
            status_counts = {
                status.value: count
                for status, count in db.execute(
                    select(Task.status, func.count(Task.id))
                    .where(Task.run_id == run_id)
                    .group_by(Task.status)
                )
            }
            progress_rows = load_run_progress(db, run_id)
            cancelable = sum(
                status_counts.get(status.value, 0)
                for status in (
                    TaskStatus.PENDING,
                    TaskStatus.QUEUED,
                    TaskStatus.RUNNING,
                    TaskStatus.BLOCKED,
                )
            )
        return templates.TemplateResponse(
            request,
            "runs/detail.html",
            context(
                request,
                active_nav="runs",
                run=run,
                playlist_name=playlist_name or "—",
                tasks=tasks,
                status_counts=status_counts,
                page=page,
                pages=max(1, (total + per_page - 1) // per_page),
                total=total,
                status_filter=status_filter,
                task_statuses=[value.value for value in TaskStatus],
                progress_rows=progress_rows,
                cancelable_count=cancelable,
                cancelable_statuses={
                    TaskStatus.PENDING.value,
                    TaskStatus.QUEUED.value,
                    TaskStatus.RUNNING.value,
                    TaskStatus.BLOCKED.value,
                },
                now=datetime.now(UTC),
            ),
        )

    def load_run_progress(db: Session, run_id: int) -> list[dict[str, Any]]:
        tasks = list(
            db.scalars(
                select(Task)
                .where(Task.run_id == run_id, Task.status == TaskStatus.RUNNING)
                .order_by(Task.started_at, Task.id)
            )
        )
        target_ids = {task.target_ref_id for task in tasks if task.target_ref_type == "target"}
        playlist_ids = {task.target_ref_id for task in tasks if task.target_ref_type == "playlist"}
        target_labels = {
            target_id: title or source_id
            for target_id, title, source_id in db.execute(
                select(Target.id, Item.title, Item.source_id)
                .join(Item, Item.id == Target.item_id)
                .where(Target.id.in_(target_ids))
            )
        }
        playlist_labels = {
            playlist_id: name
            for playlist_id, name in db.execute(
                select(Playlist.id, Playlist.name).where(Playlist.id.in_(playlist_ids))
            )
        }
        rows = []
        for task in tasks:
            progress = (task.payload_json or {}).get("progress", {})
            if not isinstance(progress, dict):
                progress = {}
            if task.target_ref_type == "target":
                label = target_labels.get(task.target_ref_id, f"Target #{task.target_ref_id}")
            elif task.target_ref_type == "playlist":
                label = playlist_labels.get(task.target_ref_id, f"Playlist #{task.target_ref_id}")
            else:
                label = f"{task.target_ref_type} #{task.target_ref_id}"
            rows.append({"task": task, "target_label": label, "progress": progress})
        return rows

    @app.get("/runs/{run_id}/progress")
    def run_progress(request: Request, run_id: int) -> Response:
        with session_factory() as db:
            if db.get(Run, run_id) is None:
                raise HTTPException(status_code=404)
            progress_rows = load_run_progress(db, run_id)
        return templates.TemplateResponse(
            request,
            "runs/_progress.html",
            context(
                request,
                active_nav="runs",
                run_id=run_id,
                progress_rows=progress_rows,
            ),
        )

    def resolve_run_log(run: Run) -> Path:
        if not run.log_path:
            raise HTTPException(status_code=404, detail="このRunにログはありません")
        data_root = settings.DATA_DIR.resolve(strict=False)
        candidate = Path(run.log_path)
        if not candidate.is_absolute():
            candidate = data_root / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            raise HTTPException(status_code=404, detail="ログファイルが見つかりません") from None
        if not resolved.is_relative_to(data_root) or not resolved.is_file():
            raise HTTPException(status_code=403, detail="ログパスがDATA_DIR境界外です")
        return resolved

    def load_run_and_log(run_id: int) -> tuple[Run, Path]:
        with session_factory() as db:
            run = db.get(Run, run_id)
            if run is None:
                raise HTTPException(status_code=404)
            return run, resolve_run_log(run)

    @app.get("/runs/{run_id}/log")
    def run_log(request: Request, run_id: int, lines: int = 500) -> Response:
        run, path = load_run_and_log(run_id)
        limit = min(2000, max(1, lines))
        try:
            with path.open(encoding="utf-8", errors="replace") as source:
                tail = "".join(deque(source, maxlen=limit))
        except OSError:
            raise HTTPException(status_code=404, detail="ログファイルを読めません") from None
        return templates.TemplateResponse(
            request,
            "runs/log.html",
            context(
                request,
                active_nav="runs",
                run=run,
                log_text=mask_log_text(tail),
                lines=limit,
            ),
        )

    @app.get("/runs/{run_id}/log/download")
    def run_log_download(run_id: int) -> Response:
        _run, path = load_run_and_log(run_id)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise HTTPException(status_code=404, detail="ログファイルを読めません") from None
        return Response(
            mask_log_text(content),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="run-{run_id}.log"'},
        )

    def sync_cancelled_targets(db: Session, run_id: int) -> None:
        for task in db.scalars(
            select(Task).where(Task.run_id == run_id, Task.status == TaskStatus.CANCELLED)
        ):
            sync_target_after_task(db, task, TaskStatus.CANCELLED)

    @app.post("/tasks/{task_id}/cancel")
    def task_cancel(request: Request, task_id: int) -> Response:
        with session_factory() as db:
            task = db.get(Task, task_id)
            if task is None:
                raise HTTPException(status_code=404)
            run_id = task.run_id
            changed = TaskRepository(db).request_cancel(task_id)
            db.refresh(task)
            if task.status == TaskStatus.CANCELLED:
                sync_target_after_task(db, task, TaskStatus.CANCELLED)
                if run_id is not None:
                    sync_cancelled_targets(db, run_id)
        auth.add_flash(
            request.state.auth,
            "success" if changed else "error",
            (
                f"Task {task_id}のキャンセルを要求しました"
                if changed
                else f"Task {task_id}はキャンセルできる状態ではありません"
            ),
        )
        return RedirectResponse(
            f"/runs/{run_id}" if run_id is not None else "/runs", status_code=303
        )

    @app.post("/runs/{run_id}/cancel")
    def run_cancel(request: Request, run_id: int) -> Response:
        with session_factory() as db:
            run = db.get(Run, run_id)
            if run is None:
                raise HTTPException(status_code=404)
            changed = TaskRepository(db).request_cancel_run(run_id)
            if changed:
                RunRepository(db).finish(
                    run_id,
                    RunStatus.CANCELLED,
                    dict(run.stats_json or {}),
                )
                sync_cancelled_targets(db, run_id)
        auth.add_flash(
            request.state.auth,
            "success" if changed else "error",
            (
                f"Run {run_id}の{changed}件へキャンセルを要求しました"
                if changed
                else f"Run {run_id}にキャンセル可能なTaskはありません"
            ),
        )
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/reports")
    def reports(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "placeholder.html",
            context(request, active_nav="reports", title="レポート", phase="Phase 13"),
        )

    @app.get("/settings")
    def settings_page(request: Request) -> Response:
        return settings_page_response(request)

    def display_setting_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (bool, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def settings_page_response(
        request: Request, *, error: str | None = None, status_code: int = 200
    ) -> Response:
        with session_factory() as db:
            groups: dict[str, list[dict[str, Any]]] = {}
            for entry in core_settings.list_all(db):
                spec = core_settings.CODE_DEFAULTS[entry.key]
                prefix = entry.key.split(".", 1)[0]
                groups.setdefault(prefix, []).append(
                    {
                        "key": entry.key,
                        "value": display_setting_value(entry.value),
                        "default": display_setting_value(spec.default),
                        "type": "list[str]" if spec.type_ is list else spec.type_.__name__,
                        "is_override": entry.is_override,
                    }
                )
        return templates.TemplateResponse(
            request,
            "settings.html",
            context(request, active_nav="settings", groups=groups, error=error),
            status_code=status_code,
        )

    def parse_setting_form_value(key: str, raw: str) -> Any:
        spec = core_settings.CODE_DEFAULTS.get(key)
        if spec is None or key.startswith("_internal."):
            raise core_settings.UnknownSettingKeyError(key)
        if spec.type_ is bool:
            if raw not in {"true", "false"}:
                raise ValueError("真偽値はtrueまたはfalseを選択してください")
            return raw == "true"
        if spec.type_ is str and spec.default is None and not raw.strip():
            return None
        try:
            value = core_settings._cast(spec.type_, raw)  # noqa: SLF001 - 同じ検証規約をUIで先行適用
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{spec.type_.__name__}として解釈できません") from exc
        if isinstance(value, (int, float)) and value < 0:
            raise ValueError("数値は0以上にしてください")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("有限の数値を指定してください")
        if key in {"staging.warn_pct", "staging.stop_pct"} and value > 100:
            raise ValueError("割合は0から100の範囲にしてください")
        if key == "worker.progress_write_percent_step" and value > 100:
            raise ValueError("進捗率の刻みは100以下にしてください")
        return value

    def validate_setting_relationships(db: Session, key: str, value: Any) -> None:
        if key in {"staging.warn_pct", "staging.stop_pct"}:
            warn = value if key == "staging.warn_pct" else core_settings.get(db, "staging.warn_pct")
            stop = value if key == "staging.stop_pct" else core_settings.get(db, "staging.stop_pct")
            if not 0 <= warn < stop <= 100:
                raise ValueError("Staging閾値は0 <= warn < stop <= 100にしてください")
        if key in {"storage.free_space_warn_bytes", "storage.free_space_stop_bytes"}:
            warn = (
                value
                if key == "storage.free_space_warn_bytes"
                else core_settings.get(db, "storage.free_space_warn_bytes")
            )
            stop = (
                value
                if key == "storage.free_space_stop_bytes"
                else core_settings.get(db, "storage.free_space_stop_bytes")
            )
            if stop > warn:
                raise ValueError("Storage容量閾値はstop <= warnにしてください")

    @app.post("/settings/update")
    async def settings_update(request: Request) -> Response:
        form = await request.form()
        key = str(form.get("key", ""))
        raw = str(form.get("value", ""))
        try:
            value = parse_setting_form_value(key, raw)
            with session_factory() as db:
                validate_setting_relationships(db, key, value)
                core_settings.set_override(db, key, value)
        except core_settings.UnknownSettingKeyError:
            raise HTTPException(status_code=404) from None
        except (TypeError, ValueError) as exc:
            return settings_page_response(
                request,
                error=f"{key or '設定キー'}: {exc}",
                status_code=422,
            )
        auth.add_flash(request.state.auth, "success", f"{key}を上書きしました")
        return RedirectResponse(f"/settings#{key.split('.', 1)[0]}", status_code=303)

    @app.post("/settings/reset")
    async def settings_reset(request: Request) -> Response:
        form = await request.form()
        key = str(form.get("key", ""))
        if key not in core_settings.CODE_DEFAULTS or key.startswith("_internal."):
            raise HTTPException(status_code=404)
        with session_factory() as db:
            core_settings.unset_override(db, key)
        auth.add_flash(request.state.auth, "success", f"{key}をコード既定値へ戻しました")
        return RedirectResponse(f"/settings#{key.split('.', 1)[0]}", status_code=303)

    def cookie_page_response(
        request: Request,
        playlist: Playlist,
        *,
        error: str | None = None,
        status_code: int = 200,
    ) -> Response:
        return templates.TemplateResponse(
            request,
            "playlist_cookies.html",
            context(
                request,
                active_nav="playlists",
                playlist_id=playlist.id,
                playlist_name=playlist.name,
                cookie_configured=playlist_cookie_configured(playlist),
                cookie_enabled=playlist.cookie_enabled,
                error=error,
            ),
            status_code=status_code,
        )

    @app.get("/playlists/{playlist_id}/cookies")
    def playlist_cookies(request: Request, playlist_id: int) -> Response:
        with session_factory() as db:
            playlist = db.get(Playlist, playlist_id)
            if playlist is None:
                raise HTTPException(status_code=404)
            return cookie_page_response(request, playlist)

    @app.post("/playlists/{playlist_id}/cookies")
    async def update_playlist_cookies(request: Request, playlist_id: int) -> Response:
        form = await request.form()
        action = str(form.get("action", ""))
        confirmed = form.get("risk_confirmed") == "yes"
        with session_factory() as db:
            playlist = db.get(Playlist, playlist_id)
            if playlist is None:
                raise HTTPException(status_code=404)
            try:
                if action == "save_enable":
                    upload = form.get("cookie_file")
                    if not isinstance(upload, UploadFile) or not upload.filename:
                        raise CookieConfigurationError("Cookieファイルを選択してください")
                    raw = await upload.read(MAX_COOKIE_BYTES + 1)
                    save_playlist_cookie(
                        db,
                        playlist,
                        raw,
                        enable_confirmed=confirmed,
                    )
                    message = "Cookieを暗号化保存し、このPlaylistで有効にしました"
                elif action == "enable":
                    set_playlist_cookie_enabled(
                        db,
                        playlist,
                        True,
                        enable_confirmed=confirmed,
                    )
                    message = "Cookieを有効にしました"
                elif action == "disable":
                    set_playlist_cookie_enabled(db, playlist, False)
                    message = "Cookieを無効にしました"
                elif action == "clear":
                    clear_playlist_cookie(db, playlist)
                    message = "Cookie設定を消去しました"
                else:
                    raise CookieConfigurationError("操作が不正です")
            except CookieConfigurationError as exc:
                return cookie_page_response(
                    request,
                    playlist,
                    error=str(exc),
                    status_code=400,
                )

        identity: SessionIdentity = request.state.auth
        auth.add_flash(identity, "success", message)
        return RedirectResponse(f"/playlists/{playlist_id}/cookies", status_code=303)

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
