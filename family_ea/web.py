"""Web view: what the system actually stored. Server-rendered, basic auth, read-only for now."""

import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from .config import Settings
from .context import bucket_commitments, fmt_date, fmt_dt, fmt_due, fts_query
from .db import Database
from .people import People

TEMPLATES_DIR = Path(__file__).parent / "templates"


def build_web(settings: Settings, people: People, db: Database) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["dt"] = lambda iso: fmt_dt(iso, settings.tz)
    templates.env.filters["date"] = fmt_date
    templates.env.filters["due"] = lambda c: fmt_due(c, settings.tz)
    templates.env.filters["person"] = people.display_name
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
                    "memories": db.search_memories(fts_query(q), limit=50),
                    "commitments": db.search_commitments(q),
                },
            )
        buckets = bucket_commitments(db.open_commitments(), datetime.now(settings.tz))
        return templates.TemplateResponse(
            request,
            "index.html",
            {"q": "", "buckets": buckets, "memories": db.list_memories(limit=200)},
        )

    @app.get("/messages", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def messages(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "messages.html", {"messages": db.list_messages(limit=200)}
        )

    return app
