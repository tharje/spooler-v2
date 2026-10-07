"""The webhook payload is a public contract (docs/external-api.md, "Webhook payload").
Fields may be added within a schema_version, never removed, renamed or retyped."""

import json
import re
import uuid
from datetime import datetime, timezone

import pytest

import config
import notifiers
from notify import Notification


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(notifiers, "_request", lambda url, data=None, headers=None, method=None:
                        calls.append(json.loads(data)) or b"{}")
    config.set("webhook.url", "https://example.org/hook")
    return calls


# field -> type, for a notification without details
CONTRACT_V1 = {
    "schema_version": int, "id": str, "timestamp": str,
    "event": str, "printer_id": str, "printer": str,
    "title": str, "body": str, "priority": str, "time": str, "has_image": bool,
}


def test_payload_has_every_v1_field_with_its_type(sent):
    notifiers.send("webhook", Notification("print_paused", "p1", "T", "B", printer_name="Bench"))
    body = sent[0]
    for key, typ in CONTRACT_V1.items():
        assert key in body, f"missing {key}"
        assert isinstance(body[key], typ), f"{key} should be {typ.__name__}"
    assert body["schema_version"] == 1
    assert "details" not in body


def test_details_only_when_there_are_some(sent):
    notifiers.send("webhook", Notification("print_error", "p1", "T", extra={"code": 704}))
    assert sent[0]["details"] == {"code": 704}


def test_id_is_a_uuid_and_unique_per_event(sent):
    a, b = Notification("x", "p", "T"), Notification("x", "p", "T")
    assert a.id != b.id
    assert str(uuid.UUID(a.id)) == a.id


def test_same_event_keeps_its_id_across_sends(sent):
    n = Notification("x", "p", "T")
    notifiers.send("webhook", n)
    notifiers.send("webhook", n)
    assert sent[0]["id"] == sent[1]["id"] == n.id


def test_timestamp_is_utc_iso8601():
    n = Notification("x", "p", "T")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", n.timestamp)
    parsed = datetime.strptime(n.timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 5


def test_old_fields_still_there(sent):
    # additive change: what 2.2.x/2.3.0-beta receivers already read is unchanged
    notifiers.send("webhook", Notification("x", "p", "T", "B"))
    assert {"event", "printer_id", "printer", "title", "body", "priority", "time", "has_image"} <= set(sent[0])
