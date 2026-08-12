"""Minimal review UI. This is the human-approval gate: nothing from this
system is a finalized "finding" without either (a) being verified with
confidence >= CONFIDENCE_THRESHOLD (auto-finding, never touches this UI) or
(b) being explicitly approved here. Unverified items are shown for audit
visibility but the backend (src/review/queue.py:approve) refuses to approve
them regardless of what this UI does or doesn't render.
"""
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from src.review import queue as queue_mod

app = FastAPI(title="legal-doc-agent review queue")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/")
def index():
    return RedirectResponse(url="/queue")


@app.get("/queue")
def view_queue(request: Request):
    items = queue_mod.load_queue()
    pending = [i for i in items if i.status == "pending"]
    decided = [i for i in items if i.status != "pending"]
    pending.sort(key=lambda i: (i.reason != "unverified", -i.confidence))
    decided.sort(key=lambda i: i.reviewed_at or "", reverse=True)
    return templates.TemplateResponse(
        request, "queue.html", {"pending": pending, "decided": decided}
    )


@app.post("/queue/{item_id}/approve")
def approve_item(item_id: str, note: str = Form(default="")):
    try:
        queue_mod.approve(item_id, note=note or None)
    except (ValueError, KeyError):
        # Unverified items (or unknown ids) are refused server-side even if
        # a stale page or crafted request tries to submit an approve -- the
        # guarantee lives here, not in the button being greyed out.
        pass
    return RedirectResponse(url="/queue", status_code=303)


@app.post("/queue/{item_id}/reject")
def reject_item(item_id: str, note: str = Form(default="")):
    try:
        queue_mod.reject(item_id, note=note or None)
    except KeyError:
        pass
    return RedirectResponse(url="/queue", status_code=303)
