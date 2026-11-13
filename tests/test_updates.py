"""Guest announcements are separate from invitation sends and never reset RSVPs."""

from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

import pytest
from sqlalchemy import inspect, select

from kith.config import SendMode, get_settings
from kith.db.models import Event, Recipient, User
from kith.services import updates


def draft(db, eid, uid, **extra):
    args = dict(
        subject="Parking update", message="Use the rear entrance.", groups=["all"], recipient_ids=[]
    )
    args.update(extra)
    return updates.create_draft(db, db.get(Event, eid), db.get(User, uid), get_settings(), **args)


def deliveries(db, batch):
    from kith.db.models import UpdateDelivery

    return db.scalars(select(UpdateDelivery).where(UpdateDelivery.update_id == batch.id)).all()


def snapshot(db, eid):
    return {
        r.id: {a.key: getattr(r, a.key) for a in inspect(Recipient).column_attrs}
        for r in db.scalars(select(Recipient).where(Recipient.event_id == eid))
    }


@pytest.mark.parametrize(
    ("groups", "labels"),
    [
        (["all"], {"unanswered", "yes", "no"}),
        (["unanswered"], {"unanswered"}),
        (["yes"], {"yes"}),
        (["no"], {"no"}),
        (["yes", "no"], {"yes", "no"}),
        (["all", "yes", "no", "unanswered"], {"unanswered", "yes", "no"}),
    ],
)
def test_group_union(occasion, client, groups, labels):
    eid, _, ids = occasion
    with client.app.state.session_factory() as db:
        rows = db.scalars(select(Recipient).where(Recipient.event_id == eid)).all()
        selected = updates.select_recipients(rows, groups, [ids["yes"]] if "yes" in labels else [])
        assert {r.id for r in selected} == {ids[k] for k in labels}
        assert len(selected) == len(labels)


def test_manual_and_overlapping_groups_deduplicate_email(occasion, client):
    eid, _, ids = occasion
    with client.app.state.session_factory() as db:
        db.add(Recipient(event_id=eid, email="YES@EXAMPLE.COM", token="duplicate", status="coming"))
        db.commit()
        rows = db.scalars(select(Recipient).where(Recipient.event_id == eid)).all()
        selected = updates.select_recipients(rows, ["yes"], [ids["yes"], ids["no"]])
        assert len(selected) == 2
        assert {r.email.lower() for r in selected} == {"yes@example.com", "no@example.com"}
        assert [r.id for r in updates.select_recipients(rows, [], [ids["no"]])] == [ids["no"]]


@pytest.mark.parametrize(
    "groups,manual",
    [([], []), (["typo"], []), ([], ["foreign"]), ([], ["unsent"]), ([], ["phone"])],
)
def test_reject_invalid_selection(occasion, client, groups, manual):
    eid, _, ids = occasion
    with client.app.state.session_factory() as db:
        rows = db.scalars(select(Recipient).where(Recipient.event_id == eid)).all()
        with pytest.raises(ValueError):
            updates.select_recipients(rows, groups, [ids.get(k, k) for k in manual])


@pytest.mark.parametrize(
    "subject,message",
    [
        ("", "Body"),
        ("Hi", "  "),
        ("Hi\r\nBcc: bad@example.com", "Body"),
        ("x" * 201, "Body"),
        ("Hi", "x" * 10001),
    ],
)
def test_reject_invalid_content(occasion, client, subject, message):
    eid, uid, _ = occasion
    with client.app.state.session_factory() as db, pytest.raises(ValueError):
        draft(db, eid, uid, subject=subject, message=message)


def test_dry_run_separate_outbox_preserves_every_recipient_field(occasion, client, monkeypatch):
    eid, uid, _ = occasion
    gmail = Mock(side_effect=AssertionError("No external mail"))
    monkeypatch.setattr("kith.services.gmail.gmail_send", gmail)
    with client.app.state.session_factory() as db:
        before = snapshot(db, eid)
        batch = draft(
            db, eid, uid, subject="A <subject>", message="<script>unsafe</script>\nParking"
        )
        assert len(deliveries(db, batch)) == 3
        assert updates.deliver_update(db, batch, db.get(User, uid), get_settings()) is True
        assert updates.deliver_update(db, batch, db.get(User, uid), get_settings()) is False
        db.expire_all()
        assert snapshot(db, eid) == before
        assert {d.status for d in deliveries(db, batch)} == {"sent"}
        files = list((get_settings().outbox_dir / eid / "updates" / batch.id).glob("*.eml"))
        assert len(files) == 3
        for f in files:
            msg = BytesParser(policy=policy.default).parsebytes(f.read_bytes())
            assert msg["Subject"] == "A <subject>"
            assert not msg["Cc"] and not msg["Bcc"]
            assert "token-" in msg.get_body(preferencelist=("plain",)).get_content()
            assert "&lt;script&gt;" in msg.get_body(preferencelist=("html",)).get_content()
            assert len(msg.get_all("To")) == 1
        gmail.assert_not_called()


def test_preview_snapshot_does_not_expand_after_rsvp_changes(occasion, client):
    eid, uid, ids = occasion
    with client.app.state.session_factory() as db:
        batch = draft(db, eid, uid, groups=["yes"])
        db.get(Recipient, ids["no"]).status = "coming"
        db.commit()
        updates.deliver_update(db, batch, db.get(User, uid), get_settings())
        assert [d.recipient_id for d in deliveries(db, batch)] == [ids["yes"]]


def test_mode_change_requires_new_preview(occasion, client):
    eid, uid, _ = occasion
    with client.app.state.session_factory() as db:
        batch = draft(db, eid, uid)
        live = get_settings().model_copy(update={"send_mode": SendMode.live})
        with pytest.raises(ValueError, match="mode"):
            updates.deliver_update(db, batch, db.get(User, uid), live)
        db.refresh(batch)
        assert batch.status == "draft"


@pytest.mark.parametrize("mode", [SendMode.self_only, SendMode.live])
def test_gmail_modes_and_no_cc(occasion, client, monkeypatch, mode):
    import base64

    eid, uid, _ = occasion
    gmail = Mock(return_value={"id": "new-message", "threadId": "new-thread"})
    monkeypatch.setattr("kith.services.gmail.gmail_send", gmail)
    settings = get_settings().model_copy(update={"send_mode": mode})
    with client.app.state.session_factory() as db:
        ev, user = db.get(Event, eid), db.get(User, uid)
        user.refresh_token = "fake-token"
        batch = updates.create_draft(
            db,
            ev,
            user,
            settings,
            subject="Update",
            message="News",
            groups=["all"],
            recipient_ids=[],
        )
        updates.deliver_update(db, batch, user, settings)
        assert gmail.call_count == 3
        targets = []
        for call in gmail.call_args_list:
            msg = BytesParser(policy=policy.default).parsebytes(
                base64.urlsafe_b64decode(call.args[2])
            )
            targets.append(str(msg["To"]))
            assert not msg["Cc"] and not msg["Bcc"]
        if mode == SendMode.self_only:
            assert targets == [user.email] * 3
        else:
            assert set(targets) == {"unanswered@example.com", "yes@example.com", "no@example.com"}


def test_missing_google_token_blocks_before_claim(occasion, client, monkeypatch):
    eid, uid, _ = occasion
    gmail = Mock()
    monkeypatch.setattr("kith.services.gmail.gmail_send", gmail)
    settings = get_settings().model_copy(update={"send_mode": SendMode.live})
    with client.app.state.session_factory() as db:
        batch = updates.create_draft(
            db,
            db.get(Event, eid),
            db.get(User, uid),
            settings,
            subject="Update",
            message="News",
            groups=["all"],
            recipient_ids=[],
        )
        with pytest.raises(ValueError, match="Google"):
            updates.deliver_update(db, batch, db.get(User, uid), settings)
        assert batch.status == "draft"
        gmail.assert_not_called()


def test_partial_failure_is_visible_and_not_automatically_retried(occasion, client, monkeypatch):
    eid, uid, _ = occasion
    gmail = Mock(side_effect=[{"id": "ok"}, TimeoutError("ambiguous send"), {"id": "ok"}])
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
        states = [d.status for d in deliveries(db, batch)]
        assert states.count("sent") == 2 and states.count("uncertain") == 1
        assert updates.deliver_update(db, batch, user, settings) is False
        assert gmail.call_count == 3
        assert snapshot(db, eid) == before


def test_deleted_or_changed_recipient_is_skipped_not_redirected(occasion, client):
    eid, uid, ids = occasion
    with client.app.state.session_factory() as db:
        batch = draft(db, eid, uid)
        db.delete(db.get(Recipient, ids["yes"]))
        db.get(Recipient, ids["no"]).email = "replacement@example.com"
        db.commit()
        updates.deliver_update(db, batch, db.get(User, uid), get_settings())
        statuses = [d.status for d in deliveries(db, batch)]
        assert statuses.count("sent") == 1 and statuses.count("skipped") == 2


def test_persisted_message_and_addresses_are_encrypted(occasion, client):
    import sqlite3

    eid, uid, _ = occasion
    with client.app.state.session_factory() as db:
        batch = draft(db, eid, uid)
        with sqlite3.connect(get_settings().db_path) as conn:
            raw = conn.execute(
                "SELECT subject, message FROM guest_updates WHERE id=?", (batch.id,)
            ).fetchone()
            assert "Parking" not in raw[0] and "entrance" not in raw[1]
            addresses = conn.execute("SELECT email FROM update_deliveries").fetchall()
            assert all("@example.com" not in a[0] for a in addresses)
