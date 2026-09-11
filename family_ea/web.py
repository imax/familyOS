"""Web view: what the system actually stored. Server-rendered; identity comes from the bot.

There is no password. `/web` in Telegram (and «Відкрити» under the digest and /today) sends
a member a link to `/login?t=…`; opening it sets a long-lived signed cookie. Read-only except
`/facts` and `/family`, the two things a human edits by hand, and the order of undated
commitments, dragged on the home page. Commitments are closed only through the LLM's
`close` op (web done/drop was removed).
"""

import json
import logging
import tempfile
from datetime import datetime
from itertools import groupby
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .auth import SESSION_TTL, sign, verify
from .config import Settings
from .context import (
    build_timeline,
    fmt_date,
    fmt_dt,
    fmt_due,
    fmt_event_when,
    fts_query,
    group_by_month,
    today_blocks,
    word_pattern,
)
from .db import Database, Member
from .family import Family
from .ical import commitment_ics, event_ics, ics_filename

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
SESSION_COOKIE = "session"
DONE_SHOWN = 10  # the «Зроблено» tail of the home page


class NotLoggedIn(Exception):
    """Rendered as a small page telling the person to ask the bot for a link."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def build_web(settings: Settings, family: Family, db: Database) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["dt"] = lambda iso: fmt_dt(iso, settings.tz)
    templates.env.filters["date"] = fmt_date
    templates.env.filters["day"] = lambda iso: fmt_dt(iso, settings.tz)[:5]
    templates.env.filters["due"] = lambda c: fmt_due(c, settings.tz)
    templates.env.filters["when"] = lambda e: fmt_event_when(e, settings.tz)
    templates.env.filters["person"] = family.display_name
    templates.env.filters["pretty_json"] = lambda s: (
        json.dumps(json.loads(s), ensure_ascii=False, indent=2) if s else ""
    )

    def secret() -> str:
        if not settings.web_secret:
            raise HTTPException(status_code=503, detail="web auth not configured: set WEB_SECRET")
        return settings.web_secret

    def member_from_cookie(request: Request) -> Member | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not settings.web_secret or not token:
            return None
        subject = verify(settings.web_secret, token, "session")
        return family.get(subject) if subject else None

    def authed(request: Request) -> Member:
        secret()
        member = member_from_cookie(request)
        if member is None:
            raise NotLoggedIn("Щоб увійти, напиши боту /web.", 401)
        return member

    @app.exception_handler(NotLoggedIn)
    async def not_logged_in(request: Request, exc: NotLoggedIn) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "login.html", {"message": exc.message}, status_code=exc.status_code
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/login")
    async def login(
        request: Request, t: str = "", next_path: Annotated[str, Query(alias="next")] = "/"
    ) -> Response:
        """The link the bot sent: set the session cookie and go where the link pointed.

        Someone already logged in on this browser stays who they are, whatever the link
        says; that is how the digest button keeps working after its link expired.
        """
        key = secret()
        target = next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"
        if member_from_cookie(request) is not None:
            return RedirectResponse(target, status_code=303)
        subject = verify(key, t, "link")
        member = family.get(subject) if subject else None
        if member is None:
            raise NotLoggedIn("Посилання застаріло. Напиши боту /web, він дасть нове.", 403)
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            sign(key, "session", member.id, SESSION_TTL),
            max_age=int(SESSION_TTL.total_seconds()),
            httponly=True,
            secure=bool(settings.web_url and settings.web_url.startswith("https")),
            samesite="lax",
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request, member: Annotated[Member, Depends(authed)], q: str | None = None
    ) -> HTMLResponse:
        """The boards (the viewer's own first), the timeline, the last done commitments;
        `?q=` searches instead."""
        if q and q.strip():
            q = q.strip()
            pattern = word_pattern(q)
            return templates.TemplateResponse(
                request,
                "search.html",
                {
                    "q": q,
                    "events": db.search_events(pattern) if pattern else [],
                    "commitments": db.search_commitments(pattern) if pattern else [],
                    "entries": db.search_entries(fts_query(q), limit=50),
                    "items": db.search_items(pattern, limit=50) if pattern else [],
                },
            )
        now = datetime.now(settings.tz)
        timeline = build_timeline(
            db.planned_events(), db.open_commitments(), db.pending_reminders(), now, family
        )
        boards = today_blocks(db.current_today_lists(), family, member.id, now)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "q": "",
                "timeline": timeline,
                "today": boards,
                "done": db.recent_done_commitments(DONE_SHOWN),
            },
        )

    @app.get("/journal", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def journal(request: Request) -> HTMLResponse:
        """What happened, by month, newest first."""
        return templates.TemplateResponse(
            request, "journal.html", {"months": group_by_month(db.list_entries(limit=200))}
        )

    @app.get("/items", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def items_page(
        request: Request, place: str | None = None, owner: str | None = None
    ) -> HTMLResponse:
        """Where things are: places with counts and what changed lately; `?place=` (empty:
        no place known) lists a place by spot, `?owner=` one person's things."""
        if place is None and not owner:
            return templates.TemplateResponse(
                request,
                "items.html",
                {"recent": db.recent_items(10), "places": db.places()},
            )
        items = db.list_items(place=place, owner=owner)
        if place is not None:
            title = place or "Без місця"
            groups = [(spot, list(g)) for spot, g in groupby(items, key=lambda i: i.spot)]
        else:
            title = f"Речі: {owner}"
            groups = [(None, items)] if items else []
        return templates.TemplateResponse(request, "place.html", {"title": title, "groups": groups})

    @app.get("/items/{iid:int}", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def item_page(request: Request, iid: int) -> HTMLResponse:
        item = db.get_item(iid)
        if item is None:
            raise HTTPException(status_code=404, detail="no such item")
        return templates.TemplateResponse(
            request, "item.html", {"item": item, "history": db.item_history(iid)}
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

    @app.post("/commitments/order", dependencies=[Depends(authed)])
    async def commitments_order(ids: Annotated[list[int], Form()]) -> Response:
        """The «Без дати» list after a drag: every id in its new place. The one thing about
        a commitment the web writes; the LLM never sets the order."""
        db.reorder_commitments(ids)
        return Response(status_code=204)

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

    @app.get("/backup.db")
    async def backup(request: Request) -> Response:
        """The whole database as one consistent file; `python -m family_ea pull` fetches it
        with a `backup` token it signs itself, sent as a bearer token."""
        key = secret()
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or verify(key, token.strip(), "backup") is None:
            raise HTTPException(status_code=401, detail="unauthorized")
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
