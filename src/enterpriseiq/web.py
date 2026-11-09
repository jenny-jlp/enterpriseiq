"""Same-origin local workbench API. HTTP requests never execute the investigation graph."""

from contextlib import asynccontextmanager, contextmanager
from decimal import Decimal
import os
from pathlib import Path
import secrets
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
import psycopg

from enterpriseiq.jobs import Jobs
from enterpriseiq.web_auth import Identity, identity_binding, load_users, token_hash


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(Body):
    token: str = Field(min_length=1, max_length=256)


class Investigation(Body):
    request_id: UUID
    question: str = Field(min_length=1, max_length=4000)
    case_id: UUID | None = None


class Resume(Body):
    request_id: UUID
    clarification: str | None = Field(default=None, min_length=1, max_length=2000)


class Review(Body):
    request_id: UUID
    report_id: UUID
    action: Literal["accept", "reject", "modify"]
    reason: str = Field(min_length=1, max_length=2000)
    opinion: str = Field(default="", max_length=4000)
    draft: dict | None = None


def create_app(*, connect=None, users=None, static_directory=None):
    if connect is None:
        @contextmanager
        def connect():
            # Mutating services own their transactions and commit before returning 202.
            # A dependency finalizer must never be responsible for committing an ACK.
            with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
                yield conn
    users = load_users() if users is None else users

    @asynccontextmanager
    async def lifespan(app):
        with connect() as conn:
            Jobs(conn).initialize()
        yield

    api = FastAPI(title="EnterpriseIQ Workbench", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @api.middleware("http")
    async def same_origin(request, call_next):
        origin = request.headers.get("origin")
        expected_origin = os.environ.get("ENTERPRISEIQ_ORIGIN", str(request.base_url).rstrip("/"))
        if request.method not in ("GET", "HEAD", "OPTIONS") and (
            (origin and origin != expected_origin) or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            return JSONResponse({"detail": "Cross-origin writes are not permitted"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @api.exception_handler(ValueError)
    def invalid_operation(request, exc):
        # ValueError messages here are domain codes/messages, never exception reprs.
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @api.exception_handler(psycopg.Error)
    def database_error(request, exc):
        return JSONResponse({"detail": "Database operation unavailable; retry using the same request ID"}, status_code=503)

    def connection():
        with connect() as conn:
            yield conn

    def identity(request: Request, conn=Depends(connection)):
        credential = request.cookies.get("enterpriseiq_session")
        row = conn.execute("SELECT credential_hash,identity_binding FROM investigation_web.sessions WHERE token_hash=%s AND expires_at>now()", (token_hash(credential or ""),)).fetchone()
        user = users.get(row[0]) if row else None
        if not user or row[1] != identity_binding(user):
            raise HTTPException(401, "Sign in to the workbench")
        return user

    def role(name):
        def require(user: Identity = Depends(identity)):
            if name not in user.roles:
                raise HTTPException(403, "This operation requires the " + name + " role")
            return user
        return require

    @api.post("/api/session")
    def login(body: Login, response: Response, conn=Depends(connection)):
        credential = token_hash(body.token)
        user = users.get(credential)
        if not user:
            raise HTTPException(401, "Invalid login token")
        session = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO investigation_web.sessions(token_hash,credential_hash,identity_binding) VALUES (%s,%s,%s)", (token_hash(session), credential, identity_binding(user)))
        response.set_cookie("enterpriseiq_session", session, httponly=True, samesite="strict", secure=os.environ.get("ENTERPRISEIQ_COOKIE_SECURE", "0") == "1", max_age=28800, path="/")
        return user.public()

    @api.delete("/api/session")
    def logout(request: Request, response: Response, conn=Depends(connection)):
        conn.execute("DELETE FROM investigation_web.sessions WHERE token_hash=%s", (token_hash(request.cookies.get("enterpriseiq_session", "")),))
        response.delete_cookie("enterpriseiq_session", path="/")
        return {"signed_out": True}

    @api.get("/api/me")
    def me(user=Depends(identity)):
        return user.public()

    @api.get("/api/cases")
    def case_list(user=Depends(identity), conn=Depends(connection)):
        return Jobs(conn).list_cases(user)

    @api.get("/api/cases/{case_id}")
    def case_show(case_id: UUID, user=Depends(identity), conn=Depends(connection)):
        return Jobs(conn).show_case(user, case_id)

    @api.post("/api/jobs", status_code=202)
    def submit(body: Investigation, user=Depends(role("investigator")), conn=Depends(connection)):
        return Jobs(conn).enqueue(user, body.request_id, body.question, body.case_id)

    @api.get("/api/jobs/{job_id}")
    def job_show(job_id: UUID, user=Depends(identity), conn=Depends(connection)):
        jobs = Jobs(conn)
        return jobs.get(user, job_id)

    @api.post("/api/runs/{run_id}/resume", status_code=202)
    def resume(run_id: UUID, body: Resume, user=Depends(role("investigator")), conn=Depends(connection)):
        return Jobs(conn).resume(user, body.request_id, run_id, body.clarification)

    @api.post("/api/cases/{case_id}/review")
    def review(case_id: UUID, body: Review, user=Depends(role("reviewer")), conn=Depends(connection)):
        return Jobs(conn).review(user, case_id, body)

    @api.get("/api/runs/{run_id}/evidence/{evidence_id}")
    def evidence(run_id: UUID, evidence_id: str, user=Depends(identity), conn=Depends(connection)):
        return JSONResponse(jsonable_encoder(Jobs(conn).evidence(user, run_id, evidence_id), custom_encoder={Decimal: str}))

    directory = Path(static_directory or os.environ.get("ENTERPRISEIQ_WEB_DIST", "web/dist"))
    if directory.is_dir():
        api.mount("/", StaticFiles(directory=directory, html=True), name="workbench")
    return api


def app():
    """uvicorn enterpriseiq.web:app --factory; fails closed without configured users."""
    return create_app()
