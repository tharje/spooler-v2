"""features.py — registry, dependency enforcement, and the @requires_feature gate."""

import json

import pytest

import features
from features import FeatureError, is_enabled, on_change, requires_feature, set_enabled


# Features that must be switched on deliberately (they open Spooler to other programs).
DEFAULT_OFF = {"external_api"}


def test_all_registered_features_default_on_except_the_opt_in_ones():
    for key in features.FEATURES:
        assert is_enabled(key) is (key not in DEFAULT_OFF), key


def test_is_enabled_rejects_unknown_key():
    with pytest.raises(FeatureError):
        is_enabled("not_a_real_feature")


def test_set_enabled_persists_across_reads():
    set_enabled("camera", False)
    assert is_enabled("camera") is False
    # Re-reading from scratch (not just a cached value) -- confirms it's
    # actually written to features.json, not just held in memory.
    assert json.loads(features.FEATURES_FILE.read_text())["camera"] is False


def test_set_enabled_missing_key_falls_back_to_default():
    features.FEATURES_FILE.write_text(json.dumps({"camera": False}))
    assert is_enabled("camera") is False
    assert is_enabled("spoolman") is True  # never mentioned in the file -> default


def test_cannot_enable_dependent_while_requirement_is_off():
    set_enabled("notifications", False)
    with pytest.raises(FeatureError, match="requires"):
        set_enabled("notify_webpush", True)


def test_disabling_requirement_cascades_to_dependents():
    assert is_enabled("notify_webpush") is True
    cascaded = set_enabled("notifications", False)
    assert "notify_webpush" in cascaded
    assert is_enabled("notify_webpush") is False


def test_re_enabling_requirement_does_not_auto_reenable_dependents():
    set_enabled("notifications", False)  # cascades notify_webpush off
    set_enabled("notifications", True)
    # Cascading off is not remembered as "temporarily off" -- once a
    # dependent is turned off, it stays off until explicitly re-enabled.
    assert is_enabled("notify_webpush") is False
    set_enabled("notify_webpush", True)  # now allowed again
    assert is_enabled("notify_webpush") is True


def test_force_disable_env_var_overrides_stored_value(monkeypatch):
    monkeypatch.setattr(features, "_FORCE_DISABLED", {"camera"})
    assert is_enabled("camera") is False
    assert any(f["key"] == "camera" and f["locked"] for f in features.describe_all())


def test_force_disabled_feature_cannot_be_enabled(monkeypatch):
    monkeypatch.setattr(features, "_FORCE_DISABLED", {"camera"})
    with pytest.raises(FeatureError, match="locked"):
        set_enabled("camera", True)


def test_on_change_fires_with_new_state():
    seen = []
    on_change("camera", lambda enabled: seen.append(enabled))
    set_enabled("camera", False)
    assert seen == [False]
    set_enabled("camera", True)
    assert seen == [False, True]


def test_on_change_fires_for_cascaded_dependents_too():
    seen = []
    on_change("notify_webpush", lambda enabled: seen.append(enabled))
    set_enabled("notifications", False)  # cascades notify_webpush off
    assert seen == [False]


def test_describe_all_shape():
    entries = features.describe_all()
    keys = {e["key"] for e in entries}
    assert keys == set(features.FEATURES)
    for e in entries:
        assert set(e) == {"key", "name", "description", "enabled", "locked", "missing", "risky", "requires"}


# ── requires_feature decorator ───────────────────────────────────────────────

class _FakeHandler:
    def __init__(self):
        self.json_calls = []

    def _json(self, data, code=200):
        self.json_calls.append((data, code))


def test_requires_feature_blocks_call_when_disabled():
    set_enabled("camera", False)

    @requires_feature("camera")
    def handler(self):
        self.called = True

    fake = _FakeHandler()
    fake.called = False
    handler(fake)

    assert fake.called is False
    assert fake.json_calls == [({"error": "feature_disabled", "feature": "camera"}, 403)]


def test_requires_feature_calls_through_when_enabled():
    @requires_feature("camera")
    def handler(self, x):
        self.called_with = x
        return "result"

    fake = _FakeHandler()
    result = handler(fake, 42)

    assert result == "result"
    assert fake.called_with == 42
    assert fake.json_calls == []


def test_requires_feature_marks_the_gated_key_for_introspection():
    @requires_feature("spoolman")
    def handler(self):
        pass

    assert handler._feature_gate == "spoolman"


def test_http_handler_gated_methods_reference_real_features():
    """Walks every method on SPHandler decorated with @requires_feature and
    confirms it points at a feature that's actually registered -- catches a
    typo'd feature key that would otherwise silently always-403 or
    always-allow (is_enabled() raises on unknown keys, which would surface as
    a 500 rather than the intended 403, so this is worth pinning down)."""
    import http_handler
    gated = [
        name for name in dir(http_handler.SPHandler)
        if hasattr(getattr(http_handler.SPHandler, name), "_feature_gate")
    ]
    assert gated, "expected at least one @requires_feature-decorated method"
    for name in gated:
        key = getattr(http_handler.SPHandler, name)._feature_gate
        assert key in features.FEATURES, f"{name} is gated on unknown feature {key!r}"
