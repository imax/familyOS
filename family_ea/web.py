"""Web view: what the system actually stored. Server-rendered, basic auth.

Read-only except `/facts` and `/family`, the two things a human edits by hand.
Commitments are closed only through the LLM's `close` op (web done/drop was removed).
"""

import json
import logging
import secrets
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from .config import Settings
from .context import (
    build_timeline,
    fmt_date,
    fmt_dt,
    fmt_due,
    fmt_event_when,
    fts_query,
)
from .db import Database
from .family import Family
from .ical import commitment_ics, event_ics, ics_filename

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def build_web(settings: Settings, family: Family, db: Database) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["dt"] = lambda iso: fmt_dt(iso, settings.tz)
    templates.env.filters["date"] = fmt_date
    templates.env.filters["due"] = lambda c: fmt_due(c, settings.tz)
    templates.env.filters["when"] = lambda e: fmt_event_when(e, settings.tz)
    templates.env.filters["person"] = family.display_name
    templates.env.filters["pretty_json"] = lambda s: (
        json.dumps(json.loads(s), ensure_ascii=False, indent=2) if s else ""
    )

    security = HTTPBasic(realm="family")

    def authed(credentials: Annotated[HTTPBasicCredentials, Depends(security)]) -> None:
        if not settings.web_user or not settings.web_password:
            raise HTTPException(status_code=503, detail="web auth not configured")
        user_ok = secrets.compare_digest(credentials.username.encode(), settings.web_user.encode())
        pass_ok = secrets.compare_digest(
            credentials.password.encode(), settings.web_password.encode()
        )
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Basic"}
            )

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def index(request: Request, q: str | None = None) -> HTMLResponse:
        if q and q.strip():
            q = q.strip()
            return templates.TemplateResponse(
                request,
                "search.html",
                {
                    "q": q,
                    "events": db.search_events(q),
                    "commitments": db.search_commitments(q),
                    "memories": db.search_memories(fts_query(q), limit=50),
                },
            )
        timeline = build_timeline(
            db.planned_events(),
            db.open_commitments(),
            db.pending_reminders(),
            datetime.now(settings.tz),
            family,
        )
        return templates.TemplateResponse(request, "index.html", {"q": "", "timeline": timeline})

    @app.get("/memories", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def memories_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "memories.html", {"memories": db.list_memories(limit=200)}
        )

    def ics_response(data: bytes, text: str) -> Response:
        """Open the file and the phone calendar offers to add the event."""
        return Response(
            data,
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{ics_filename(text)}"'},
        )

    @app.get("/events/{eid:int}.ics", dependencies=[Depends(authed)])
    async def event_ics_file(eid: int) -> Response:
        e = db.get_event(eid)
        if e is None:
            raise HTTPException(status_code=404, detail="no such event")
        return ics_response(event_ics(e), e.text)

    @app.get("/commitments/{cid:int}.ics", dependencies=[Depends(authed)])
    async def commitment_ics_file(cid: int) -> Response:
        c = db.get_commitment(cid)
        if c is None or not c.has_due:
            raise HTTPException(status_code=404, detail="no such dated commitment")
        return ics_response(commitment_ics(c), c.text)

    @app.get("/facts", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def facts_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "facts.html",
            {"facts": db.current_facts(), "versions": db.facts_versions()},
        )

    @app.post("/facts", dependencies=[Depends(authed)])
    async def facts_save(text: Annotated[str, Form()] = "") -> RedirectResponse:
        text = text.replace("\r\n", "\n").strip()
        current = db.current_facts()
        if text != (current.text if current else ""):
            db.save_facts(text, "web")
        return RedirectResponse("/facts", status_code=303)

    @app.get("/family", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def family_page(
        request: Request, name: str = "", telegram_id: str = "", error: str = ""
    ) -> HTMLResponse:
        """`name` and `telegram_id` prefill the add form (the bot links here with them)."""
        return templates.TemplateResponse(
            request,
            "family.html",
            {
                "members": family.members,
                "admin_id": family.admin_telegram_id,
                "prefill": {"name": name, "telegram_id": telegram_id},
                "error": error,
            },
        )

    @app.post("/family", dependencies=[Depends(authed)])
    async def family_save(
        member_id: Annotated[str, Form(alias="id")] = "",
        name: Annotated[str, Form()] = "",
        telegram_id: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Add a member (no id) or update one (id given). Errors go back as `?error=`."""

        def failed(message: str) -> RedirectResponse:
            return RedirectResponse("/family?" + urlencode({"error": message}), status_code=303)

        tg_raw = telegram_id.strip()
        if tg_raw and not tg_raw.isdigit():
            return failed("Telegram id — це число.")
        tg = int(tg_raw) if tg_raw else None
        try:
            if member_id:
                if not family.update(member_id, name=name, telegram_id=tg):
                    raise HTTPException(status_code=404, detail="no such member")
            else:
                family.add(name, tg)
        except ValueError as exc:
            log.info("family form rejected: %s", exc)
            return failed("Не збережено: порожнє ім'я або такий Telegram id уже є.")
        return RedirectResponse("/family", status_code=303)

    @app.get("/messages", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def messages(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "messages.html", {"messages": db.list_messages(limit=200)}
        )

    @app.get("/backup.db", dependencies=[Depends(authed)])
    async def backup() -> Response:
        """The whole database as one consistent file; `python -m family_ea pull` fetches it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "family.db"
            db.backup_to(path)
            data = path.read_bytes()
        stamp = datetime.now(settings.tz).strftime("%Y-%m-%d-%H%M")
        return Response(
            data,
            media_type="application/vnd.sqlite3",
            headers={"Content-Disposition": f'attachment; filename="family-{stamp}.db"'},
        )

    return app
