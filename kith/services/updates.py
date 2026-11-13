"""Explicit, one-shot guest announcements. Invitation/RSVP state is read-only.

Claim before dispatch and persist each attempt before contacting Gmail. An
interrupted/uncertain send is never automatically retried: external exactly-once
email delivery cannot be guaranteed. No CC, WhatsApp, reminders or attachments.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from html import escape

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from kith.config import SendMode, Settings
from kith.core import mailbuild
from kith.db.models import Event, GuestUpdate, Recipient, UpdateDelivery, User
from kith.services import gmail
from kith.services.gmail import GmailAuthError

log = logging.getLogger("kith")
GROUPS = {
    "all": "All invited guests",
    "unanswered": "Not responded",
    "yes": "RSVP yes",
    "no": "RSVP no",
}
STATUSES = {"sent", "coming", "declined"}


def eligible(recipient: Recipient) -> bool:
    return (
        recipient.status in STATUSES
        and bool(recipient.email)
        and not recipient.phone
        and recipient.channel in (None, "email")
    )


def select_recipients(
    recipients: list[Recipient],
    groups: list[str],
    recipient_ids: list[str],
) -> list[Recipient]:
    if set(groups) - GROUPS.keys():
        raise ValueError("Unknown guest group.")
    available = {r.id: r for r in recipients if eligible(r)}
    if set(recipient_ids) - available.keys():
        raise ValueError("Choose only already invited email guests from this event.")
    statuses = {
        s for g, s in (("unanswered", "sent"), ("yes", "coming"), ("no", "declined")) if g in groups
    }
    selected = []
    addresses = set()
    for r in available.values():
        address = r.email.strip().casefold()
        if (
            "all" in groups or r.status in statuses or r.id in recipient_ids
        ) and address not in addresses:
            selected.append(r)
            addresses.add(address)
    if not selected:
        raise ValueError("Select at least one guest group or individual guest.")
    if len(selected) > 500:
        raise ValueError("Preview at most 500 email recipients per update.")
    return selected


def create_draft(
    db: Session,
    event: Event,
    user: User,
    settings: Settings,
    *,
    subject: str,
    message: str,
    groups: list[str],
    recipient_ids: list[str],
) -> GuestUpdate:
    if event.user_id != user.id:
        raise ValueError("Event is not owned by this user.")
    if (
        not subject.strip()
        or len(subject) > 200
        or "\r" in subject
        or "\n" in subject
        or not message.strip()
        or len(message) > 10000
    ):
        raise ValueError("Enter a single-line subject (1–200 characters) and message (1–10,000).")
    rows = list(
        db.scalars(
            select(Recipient)
            .where(Recipient.event_id == event.id)
            .order_by(Recipient.created_at, Recipient.id)
        )
    )
    recipients = select_recipients(rows, groups, recipient_ids)
    batch = GuestUpdate(
        event_id=event.id,
        subject=subject.strip(),
        message=message.strip(),
        mode=settings.send_mode.value,
    )
    db.add(batch)
    db.flush()
    for r in recipients:
        db.add(
            UpdateDelivery(
                update_id=batch.id, recipient_id=r.id, email=r.email.strip(), name=r.name
            )
        )
    db.commit()
    return batch


def deliver_update(db: Session, batch: GuestUpdate, user: User, settings: Settings) -> bool:
    event = db.get(Event, batch.event_id)
    if event is None or event.user_id != user.id:
        raise ValueError("Event is not owned by this user.")
    if batch.status != "draft":
        return False
    if batch.mode != settings.send_mode.value:
        raise ValueError("Send mode changed. Compose a new preview before confirming.")
    refresh_token = user.refresh_token or ""
    if settings.send_mode != SendMode.dry_run and not refresh_token:
        raise ValueError("Connect Google before confirming email delivery.")
    claimed = db.execute(
        update(GuestUpdate)
        .where(GuestUpdate.id == batch.id, GuestUpdate.status == "draft")
        .values(status="sending")
        .returning(GuestUpdate.id)
    ).scalar_one_or_none()
    db.commit()
    if claimed is None:
        return False
    db.refresh(batch)
    rows = list(
        db.scalars(
            select(UpdateDelivery)
            .where(UpdateDelivery.update_id == batch.id)
            .order_by(UpdateDelivery.id)
        )
    )
    auth_failed = False
    for delivery in rows:
        r = db.get(Recipient, delivery.recipient_id) if delivery.recipient_id else None
        if (
            auth_failed
            or r is None
            or r.event_id != event.id
            or not eligible(r)
            or r.email.strip().casefold() != delivery.email.casefold()
        ):
            delivery.status = "skipped"
            delivery.error = (
                "Google reconnect required" if auth_failed else "Guest changed or removed"
            )
            db.commit()
            continue
        view_url = f"{settings.base_url.rstrip('/')}/i/{r.token}"
        text = f"{batch.message}\n\n{event.title}\nView your invitation: {view_url}\n"
        html = (
            f"<p>{escape(batch.message).replace(chr(10), '<br>')}</p>"
            f"<p>{escape(event.title)}</p>"
            f'<p><a href="{escape(view_url, quote=True)}">View your invitation</a></p>'
        )
        # Persist the attempt BEFORE the external side effect. Do not auto-resume
        # 'sending' after a crash; the provider may already have accepted it.
        delivery.status = "sending"
        db.commit()
        contacted_provider = False
        try:
            msg = mailbuild.build_email(
                subject=batch.subject,
                from_name=user.display_name,
                from_email=user.email,
                to_email=user.email if settings.send_mode == SendMode.self_only else delivery.email,
                html=html,
                text=text,
                message_id=mailbuild.make_message_id(user.email.rsplit("@", 1)[-1]),
            )
            if settings.send_mode == SendMode.dry_run:
                outbox = settings.outbox_dir / event.id / "updates" / batch.id
                outbox.mkdir(parents=True, exist_ok=True)
                (outbox / f"{delivery.id}.eml").write_bytes(msg.as_bytes())
            else:
                contacted_provider = True
                gmail.gmail_send(settings, refresh_token, mailbuild.to_raw(msg))
            delivery.status = "sent"
            delivery.sent_at = datetime.now(UTC)
        except GmailAuthError:
            auth_failed = True
            user.reconnect_needed = True
            delivery.status, delivery.error = "failed", "Google reconnect required"
        except Exception:
            # A Gmail timeout could mean accepted-but-response-lost. Don't label
            # it safely retryable and accidentally send a second announcement.
            delivery.status = "uncertain" if contacted_provider else "failed"
            delivery.error = "Check sender's Sent folder before composing another update"
            log.warning("Guest update delivery %s ended %s", delivery.id, delivery.status)
        db.commit()
    batch.status = "complete"
    batch.finished_at = datetime.now(UTC)
    db.commit()
    return True
