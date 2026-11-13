"""Durable announcement claims, upgrade compatibility and failure reporting."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

from sqlalchemy import select
from test_updates import deliveries, draft, snapshot

from kith.config import SendMode, get_settings
from kith.db.models import Event, GuestUpdate, Recipient, UpdateDelivery, User
from kith.db.session import init_db
from kith.services import updates
from kith.services.gmail import GmailAuthError


def test_concurrent_confirmation_claims_only_once(occasion, client, monkeypatch):
    eid, uid, _ = occasion
    gmail = Mock(return_value={"id": "fake"})
    monkeypatch.setattr("kith.services.gmail.gmail_send", gmail)
    settings = get_settings().model_copy(update={"send_mode": SendMode.live})
    with client.app.state.session_factory() as db:
        user = db.get(User, uid)
        user.refresh_token = "fake-token"
        batch = updates.create_draft(
            db,
            db.get(Event, eid),
            user,
            settings,
            subject="Update",
            message="News",
            groups=["all"],
            recipient_ids=[],
        )
        bid = batch.id
    barrier = Barrier(2)

    def confirm():
        with client.app.state.session_factory() as db:
            batch = db.get(GuestUpdate, bid)
            user = db.get(User, uid)
            assert batch.status == "draft"
            barrier.wait(timeout=5)
            return updates.deliver_update(db, batch, user, settings)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(), range(2)))
    assert sorted(results) == [False, True]
    assert gmail.call_count == 3


def test_additive_tables_upgrade_without_touching_existing_data(occasion, client):
    eid, uid, _ = occasion
    engine = client.app.state.engine
    with client.app.state.session_factory() as db:
        before = snapshot(db, eid)
    # Reconstruct the pre-feature schema in this disposable test DB only.
    UpdateDelivery.__table__.drop(engine)
    GuestUpdate.__table__.drop(engine)
    init_db(engine)
    with client.app.state.session_factory() as db:
        assert snapshot(db, eid) == before
        batch = draft(db, eid, uid)
        assert len(deliveries(db, batch)) == 3


def test_google_auth_failure_stops_remaining_sends(occasion, client, monkeypatch):
    eid, uid, _ = occasion
    gmail = Mock(side_effect=GmailAuthError("fake expired token"))
    monkeypatch.setattr("kith.services.gmail.gmail_send", gmail)
    settings = get_settings().model_copy(update={"send_mode": SendMode.live})
    with client.app.state.session_factory() as db:
        user = db.get(User, uid)
        user.refresh_token = "fake-token"
        before = snapshot(db, eid)
        batch = updates.create_draft(
            db,
            db.get(Event, eid),
            user,
            settings,
            subject="Update",
            message="News",
            groups=["all"],
            recipient_ids=[],
        )
        updates.deliver_update(db, batch, user, settings)
        assert gmail.call_count == 1
        assert user.reconnect_needed
        assert [d.status for d in deliveries(db, batch)].count("failed") == 1
        assert [d.status for d in deliveries(db, batch)].count("skipped") == 2
        assert snapshot(db, eid) == before


def test_other_user_cannot_create_or_send_update(occasion, client):
    import pytest

    eid, uid, _ = occasion
    with client.app.state.session_factory() as db:
        batch = draft(db, eid, uid)
        other = User(google_sub="different-user", email="other@example.com")
        db.add(other)
        db.commit()
        with pytest.raises(ValueError, match="owned"):
            updates.create_draft(
                db,
                db.get(Event, eid),
                other,
                get_settings(),
                subject="Update",
                message="News",
                groups=["all"],
                recipient_ids=[],
            )
        with pytest.raises(ValueError, match="owned"):
            updates.deliver_update(db, batch, other, get_settings())
        assert batch.status == "draft"


def test_in_progress_batch_never_retries_after_interruption(occasion, client, monkeypatch):
    eid, uid, _ = occasion
    gmail = Mock(side_effect=AssertionError("No automatic retries"))
    monkeypatch.setattr("kith.services.gmail.gmail_send", gmail)
    with client.app.state.session_factory() as db:
        batch = draft(db, eid, uid)
        batch.status = "sending"
        rows = deliveries(db, batch)
        rows[0].status = "sending"
        db.commit()
        assert updates.deliver_update(db, batch, db.get(User, uid), get_settings()) is False
        assert batch.status == "sending" and rows[0].status == "sending"
        gmail.assert_not_called()


def test_body_and_recipients_cannot_be_replaced_on_confirmation(occasion, client):
    from test_updates_web import compose, csrf

    eid, _, _ = occasion
    url = compose(client, eid).headers["location"]
    preview = client.get(url)
    response = client.post(
        url + "/send",
        data={
            "csrf_token": csrf(preview.text),
            "subject": "Injected",
            "message": "Injected",
            "groups": ["all"],
            "recipient_ids": ["foreign"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with client.app.state.session_factory() as db:
        batch = db.scalars(select(GuestUpdate)).one()
        assert batch.subject == "Parking update" and "Do not execute" in batch.message
        assert len(deliveries(db, batch)) == 2
        assert db.scalars(select(Recipient).where(Recipient.status == "coming")).all()
