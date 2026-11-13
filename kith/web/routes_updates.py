"""Owned, CSRF-protected guest announcement composition and confirmation."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections import Counter

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from kith.config import get_settings
from kith.db.models import Event, GuestUpdate, Recipient, UpdateDelivery
from kith.services import updates
from kith.web.deps import get_db, load_user, templates

router = APIRouter()


def _owned_event(db, user, event_id):
    event = db.get(Event, event_id)
    if event is None or event.user_id != user.id:
        raise HTTPException(404, "Event not found")
    return event


def _batch(db, event_id, update_id):
    batch = db.get(GuestUpdate, update_id)
    if batch is None or batch.event_id != event_id:
        raise HTTPException(404, "Update not found")
    return batch


def _token(request, update_id=None):
    key = request.session.get("update_csrf", "")
    if not key:
        return ""
    if update_id is None:
        return key
    return hmac.new(key.encode(), update_id.encode(), hashlib.sha256).hexdigest()


def _verify(request, submitted, update_id=None):
    expected = _token(request, update_id)
    if not expected or not hmac.compare_digest(expected.encode(), submitted.encode()):
        raise HTTPException(403, "Invalid confirmation token; reload the form")


def _composer(request, db, event, user, *, error=None, values=None):
    request.session.setdefault("update_csrf", secrets.token_urlsafe(32))
    rows = list(
        db.scalars(
            select(Recipient)
            .where(Recipient.event_id == event.id)
            .order_by(Recipient.created_at, Recipient.id)
        )
    )
    history = list(
        db.scalars(
            select(GuestUpdate)
            .where(GuestUpdate.event_id == event.id)
            .order_by(GuestUpdate.created_at.desc(), GuestUpdate.id)
            .limit(10)
        )
    )
    return templates.TemplateResponse(
        request,
        "updates_compose.html",
        {
            "event": event,
            "user": user,
            "settings": get_settings(),
            "recipients": [r for r in rows if updates.eligible(r)],
            "groups": updates.GROUPS,
            "history": history,
            "csrf_token": _token(request),
            "error": error,
            "values": values or {},
        },
        status_code=400 if error else 200,
        headers={"Cache-Control": "no-store"},
    )


def _preview(request, db, event, user, batch, *, error=None):
    request.session.setdefault("update_csrf", secrets.token_urlsafe(32))
    rows = list(
        db.scalars(
            select(UpdateDelivery)
            .where(UpdateDelivery.update_id == batch.id)
            .order_by(UpdateDelivery.id)
        )
    )
    return templates.TemplateResponse(
        request,
        "updates_preview.html",
        {
            "event": event,
            "user": user,
            "settings": get_settings(),
            "batch": batch,
            "deliveries": rows,
            "counts": Counter(d.status for d in rows),
            "error": error,
            "csrf_token": _token(request, batch.id),
        },
        status_code=409 if error else 200,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/events/{event_id}/updates")
def composer(event_id: str, request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    return _composer(request, db, _owned_event(db, user, event_id), user)


@router.post("/events/{event_id}/updates/preview")
def make_preview(
    event_id: str,
    request: Request,
    db: Session = Depends(get_db),
    subject: str = Form("", max_length=200),
    message: str = Form("", max_length=10000),
    groups: list[str] = Form(default=[], max_length=16),
    recipient_ids: list[str] = Form(default=[], max_length=500),
    csrf_token: str = Form("", max_length=128),
):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    event = _owned_event(db, user, event_id)
    _verify(request, csrf_token)
    try:
        batch = updates.create_draft(
            db,
            event,
            user,
            get_settings(),
            subject=subject,
            message=message,
            groups=groups,
            recipient_ids=recipient_ids,
        )
    except ValueError as exc:
        return _composer(
            request,
            db,
            event,
            user,
            error=str(exc),
            values={
                "subject": subject,
                "message": message,
                "groups": groups,
                "recipient_ids": recipient_ids,
            },
        )
    return RedirectResponse(f"/events/{event.id}/updates/{batch.id}", status_code=303)


@router.get("/events/{event_id}/updates/{update_id}")
def show_preview(event_id: str, update_id: str, request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    event = _owned_event(db, user, event_id)
    return _preview(request, db, event, user, _batch(db, event.id, update_id))


@router.post("/events/{event_id}/updates/{update_id}/send")
def confirm(
    event_id: str,
    update_id: str,
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form("", max_length=128),
):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    event = _owned_event(db, user, event_id)
    batch = _batch(db, event.id, update_id)
    _verify(request, csrf_token, batch.id)
    try:
        updates.deliver_update(db, batch, user, get_settings())
    except ValueError as exc:
        return _preview(request, db, event, user, batch, error=str(exc))
    return RedirectResponse(f"/events/{event.id}/updates/{batch.id}", status_code=303)
