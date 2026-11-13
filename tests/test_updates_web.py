"""Owned guest-update composer -> frozen preview -> one confirmed send."""

import re
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from kith.db.models import Event, GuestUpdate, Recipient, User


def csrf(html):
    match = re.search(r'name="csrf_token" value="([A-Za-z0-9_-]+)"', html)
    assert match, "CSRF token missing"
    return match.group(1)


def compose(client, eid, **extra):
    page = client.get(f"/events/{eid}/updates")
    assert page.status_code == 200
    fields = {
        "subject": "Parking update",
        "message": "<script>Do not execute</script>",
        "groups": ["yes", "no"],
        "csrf_token": csrf(page.text),
    }
    fields.update(extra)
    return client.post(f"/events/{eid}/updates/preview", data=fields, follow_redirects=False)


def test_event_action_composer_groups_and_preview(occasion, client):
    eid, _, ids = occasion
    assert "Email guests" in client.get(f"/events/{eid}").text
    page = client.get(f"/events/{eid}/updates")
    for group in ("all", "unanswered", "yes", "no"):
        assert f'value="{group}"' in page.text
    assert ids["unsent"] not in page.text and ids["phone"] not in page.text
    assert ids["yes"] in page.text
    response = compose(client, eid, recipient_ids=[ids["yes"]])
    assert response.status_code == 303
    preview = client.get(response.headers["location"])
    assert "2 recipients" in preview.text and "yes@example.com" in preview.text
    assert "no@example.com" in preview.text and "unanswered@example.com" not in preview.text
    assert "&lt;script&gt;" in preview.text and "<script>Do not execute" not in preview.text
    assert "dry-run" in preview.text and "Confirm" in preview.text
    assert "nothing is emailed" in preview.text.lower()
    with client.app.state.session_factory() as db:
        batch = db.scalars(select(GuestUpdate)).one()
        assert batch.status == "draft"
        assert db.get(Recipient, ids["yes"]).status == "coming"


def test_confirm_one_time_and_status_history(occasion, client, monkeypatch):
    eid, _, _ = occasion
    gmail = Mock(side_effect=AssertionError("dry run"))
    monkeypatch.setattr("kith.services.gmail.gmail_send", gmail)
    response = compose(client, eid)
    url = response.headers["location"]
    preview = client.get(url)
    data = {"csrf_token": csrf(preview.text)}
    assert client.post(url + "/send", data=data, follow_redirects=False).status_code == 303
    assert client.post(url + "/send", data=data, follow_redirects=False).status_code == 303
    result = client.get(url)
    assert "2 sent" in result.text and "dry-run" in result.text
    assert "Confirm" not in result.text
    assert "Parking update" in client.get(f"/events/{eid}/updates").text
    gmail.assert_not_called()


def test_anonymous_and_other_owner_cannot_access_batch_or_send(occasion, client):
    eid, _, _ = occasion
    url = compose(client, eid).headers["location"]
    token = csrf(client.get(url).text)
    client.cookies.clear()
    for path in (f"/events/{eid}/updates", url):
        assert client.get(path, follow_redirects=False).status_code == 303
    assert (
        client.post(url + "/send", data={"csrf_token": token}, follow_redirects=False).status_code
        == 303
    )
    client.post("/auth/dev-login")
    with client.app.state.session_factory() as db:
        other = User(google_sub="other", email="other@example.com")
        db.add(other)
        db.flush()
        db.get(Event, eid).user_id = other.id
        db.commit()
    assert client.get(f"/events/{eid}/updates").status_code == 404
    assert client.get(url).status_code == 404
    assert client.post(url + "/send", data={"csrf_token": token}).status_code == 404


@pytest.mark.parametrize("bad_token", ["", "forged", "é"])
def test_csrf_required_for_preview_and_send(occasion, client, bad_token):
    eid, _, _ = occasion
    url = compose(client, eid).headers["location"]
    assert (
        client.post(
            f"/events/{eid}/updates/preview",
            data={
                "subject": "x",
                "message": "y",
                "groups": ["all"],
                "csrf_token": bad_token,
            },
        ).status_code
        == 403
    )
    assert client.post(url + "/send", data={"csrf_token": bad_token}).status_code == 403
    with client.app.state.session_factory() as db:
        assert db.scalars(select(GuestUpdate)).one().status == "draft"


def test_confirmation_token_is_bound_to_preview(occasion, client):
    eid, _, _ = occasion
    first = compose(client, eid).headers["location"]
    token = csrf(client.get(first).text)
    second = compose(client, eid).headers["location"]
    assert client.post(second + "/send", data={"csrf_token": token}).status_code == 403


@pytest.mark.parametrize(
    "extra",
    [
        {"groups": []},
        {"groups": ["invalid"]},
        {"subject": "x\nBcc: bad@example.com"},
        {"recipient_ids": ["foreign"]},
        {"message": " "},
    ],
)
def test_invalid_composition_is_rejected_without_new_draft(occasion, client, extra):
    eid, _, _ = occasion
    response = compose(client, eid, **extra)
    assert response.status_code == 400
    assert "Email guests" in response.text
    with client.app.state.session_factory() as db:
        assert list(db.scalars(select(GuestUpdate))) == []


def test_batch_from_another_event_cannot_be_substituted(occasion, client):
    eid, uid, _ = occasion
    url = compose(client, eid).headers["location"]
    with client.app.state.session_factory() as db:
        other = Event(user_id=uid, title="Other")
        db.add(other)
        db.commit()
        other_id = other.id
    substituted = url.replace(eid, other_id)
    assert client.get(substituted).status_code == 404
    assert (
        client.post(
            substituted + "/send", data={"csrf_token": csrf(client.get(url).text)}
        ).status_code
        == 404
    )


def test_mode_change_is_visible_and_blocks_confirmation(occasion, client, monkeypatch):
    from kith.config import SendMode, get_settings

    eid, _, _ = occasion
    url = compose(client, eid).headers["location"]
    preview = client.get(url)
    settings = get_settings().model_copy(update={"send_mode": SendMode.live})
    monkeypatch.setattr("kith.web.routes_updates.get_settings", lambda: settings)
    assert "mode changed" in client.get(url).text.lower()
    assert client.post(url + "/send", data={"csrf_token": csrf(preview.text)}).status_code == 409


def test_update_pages_are_not_cacheable(occasion, client):
    eid, _, _ = occasion
    assert client.get(f"/events/{eid}/updates").headers["cache-control"] == "no-store"
    url = compose(client, eid).headers["location"]
    assert client.get(url).headers["cache-control"] == "no-store"
