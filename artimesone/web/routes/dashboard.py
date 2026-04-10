"""Dashboard route — landing page."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Render the dashboard page."""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "dashboard.html", {})  # type: ignore[no-any-return]
