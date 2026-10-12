"""Browser session pages and authentication routes."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from english_vocab_trainer.web.auth import AuthenticationError
from english_vocab_trainer.web.dependencies import csrf

Csrf = Annotated[None, Depends(csrf)]
templates = Jinja2Templates(directory=str(Path(__file__).parents[1] / "templates"))
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    container = request.app.state.container
    if container.environment not in {"local", "test"}:
        try:
            assert container.auth is not None
            container.auth.validate_session(request.cookies.get(container.auth.session_cookie_name))
        except AuthenticationError:
            return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "title": "English Vocab Trainer",
            "show_logout": container.environment == "production",
        },
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"title": "Sign in", "error": False},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request) -> Response:
    container = request.app.state.container
    if container.environment in {"local", "test"} or container.auth is None:
        raise HTTPException(404, "not found")
    origin = request.headers.get("Origin")
    if origin is not None and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "invalid origin")
    # Reject a surprisingly large form before multipart parsing.  We still call
    # authenticate with a harmless value so the generic response consumes a
    # limiter reservation; an attacker cannot bypass the global limiter by
    # switching to an oversized request.
    try:
        oversized = int(request.headers.get("Content-Length", "0")) > 4_096
    except ValueError:
        oversized = True
    password: object
    if oversized:
        password = ""
    else:
        form = await request.form()
        password = form.get("password")
    cookies = container.auth.authenticate(password if isinstance(password, str) else "")
    if cookies is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"title": "Sign in", "error": True},
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )
    response = RedirectResponse("/", status_code=303, headers={"Cache-Control": "no-store"})
    common = {
        "max_age": 30 * 24 * 60 * 60,
        "secure": container.auth.secure_cookies,
        "samesite": "strict",
        "path": "/",
    }
    response.set_cookie(
        container.auth.session_cookie_name, cookies.session, httponly=True, **common
    )
    response.set_cookie(container.auth.csrf_cookie_name, cookies.csrf, httponly=False, **common)
    return response


@router.post("/auth/logout", status_code=204)
def logout(request: Request, _: Csrf) -> Response:
    container = request.app.state.container
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    if container.auth is not None:
        for name in (container.auth.session_cookie_name, container.auth.csrf_cookie_name):
            response.delete_cookie(
                name, path="/", secure=container.auth.secure_cookies, samesite="strict"
            )
    return response
