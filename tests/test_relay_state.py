"""Synthetic relay transactions and probe/routing races; never live HTTP."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

import pytest

from zcode_gateway import relay_manager as module
from zcode_gateway.relay_manager import RelayManager


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A state test must install its own probe")
    monkeypatch.setattr(module, "ping_relay", forbidden)


def entries(tmp_path):
    manager = RelayManager(tmp_path)
    first = manager.add_relay("one", "https://one.example", "cloudflare")
    second = manager.add_relay("two", "https://two.example", "vercel")
    return manager, first, second


def threaded(callback):
    results, errors = [], []
    def run():
        try:
            results.append(callback())
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    return thread, results, errors


def finish(task):
    thread, results, errors = task
    thread.join(2)
    assert not thread.is_alive()
    assert errors == []
    return results


def good(latency=12):
    return {"ok": True, "status": 200, "elapsed_ms": latency,
            "probe_kind": "forwarding", "probe_verified": True}


def test_settings_and_returned_entries_have_independent_nested_values(tmp_path):
    manager, first, _ = entries(tmp_path)
    original = manager.get_settings()
    snapshot = manager.get_settings()
    snapshot["relays"][0]["url"] = "https://changed.example"
    snapshot["models"].append("unwanted")
    first["name"] = "mutated"
    assert manager.get_settings() == original
    persisted = copy.deepcopy(original)
    manager.save_settings(persisted)
    persisted["relays"].clear()
    assert manager.get_settings() == original


@pytest.mark.parametrize("operation", ["add", "delete", "delete_many", "activate", "options", "save"])
def test_replace_failure_preserves_cache_disk_and_cooldowns(tmp_path, monkeypatch, operation):
    manager, first, second = entries(tmp_path)
    manager.mark_relay_failed(first["url"])
    manager._probe_failed[first["url"]] = 99_999_999_999
    before = manager.get_settings()
    before_disk = manager.path.read_bytes()
    before_failed = copy.deepcopy(manager._failed), copy.deepcopy(manager._probe_failed)
    def fail(*args):
        raise OSError("simulated read-only filesystem")
    monkeypatch.setattr(Path, "replace", fail)
    actions = {
        "add": lambda: manager.add_relay("three", "https://three.example"),
        "delete": lambda: manager.delete_relay(first["id"]),
        "delete_many": lambda: manager.delete_relays(all_relays=True),
        "activate": lambda: manager.set_active(second["id"]),
        "options": lambda: manager.update_options(enabled=False, models=["changed"]),
        "save": lambda: manager.save_settings(dict(before, enabled=False)),
    }
    with pytest.raises(OSError):
        actions[operation]()
    assert manager.get_settings() == before
    assert manager.path.read_bytes() == before_disk
    assert (manager._failed, manager._probe_failed) == before_failed
    assert not manager.path.with_suffix(".tmp").exists()


def test_write_failure_never_publishes_candidate(tmp_path, monkeypatch):
    manager, _, _ = entries(tmp_path)
    before = manager.get_settings()
    original = Path.write_text
    def fail(path, *args, **kwargs):
        if path.suffix == ".tmp":
            raise OSError("disk full")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", fail)
    with pytest.raises(OSError):
        manager.update_options(enabled=False)
    assert manager.get_settings() == before
    assert json.loads(manager.path.read_text()) == before


@pytest.mark.parametrize("kwargs", [
    {"models": "GLM-5.3"}, {"models": [123]}, {"enabled": "false"},
    {"enabled": False, "models": ["new"], "strategy": "bad"}, {"active_id": "deleted"},
])
def test_invalid_options_are_atomic(tmp_path, kwargs):
    manager, _, _ = entries(tmp_path)
    before, disk = manager.get_settings(), manager.path.read_bytes()
    with pytest.raises(ValueError):
        manager.update_options(**kwargs)
    assert manager.get_settings() == before
    assert manager.path.read_bytes() == disk


def test_readonly_legacy_unknown_provider_remains_json_data(tmp_path):
    data = RelayManager(tmp_path)._default_settings()
    data["relays"] = [{"id": "old", "url": "https://legacy.example", "type": "<legacy>",
                       "nested": {"values": ["keep"]}}]
    path = tmp_path / "relays.json"
    path.write_text(json.dumps(data))
    disk = path.read_bytes()
    manager = RelayManager(tmp_path)
    settings = manager.get_settings()
    assert settings["relays"][0]["type"] == "<legacy>"
    settings["relays"][0]["nested"]["values"].clear()
    assert manager.get_settings()["relays"][0]["nested"]["values"] == ["keep"]
    assert path.read_bytes() == disk
    with pytest.raises(ValueError):
        manager.add_relay("unknown", "https://new.example", "<legacy>")
    assert path.read_bytes() == disk


def test_missing_or_invalid_json_read_is_readonly(tmp_path):
    manager = RelayManager(tmp_path)
    manager.get_settings()
    assert not manager.path.exists()
    manager.path.write_text("{broken")
    other = RelayManager(tmp_path)
    assert other.get_settings() == other._default_settings()
    assert manager.path.read_text() == "{broken"


@pytest.mark.parametrize("bulk", [False, True])
def test_probe_only_merges_health_into_latest_options(tmp_path, monkeypatch, bulk):
    manager, first, second = entries(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def ping(url, **kwargs):
        if url == first["url"]:
            entered.set()
            assert release.wait(2)
        return good()
    monkeypatch.setattr(module, "ping_relay", ping)
    task = threaded(manager.ping_all if bulk else lambda: manager.test_relay_by_id(first["id"]))
    try:
        assert entered.wait(2)
        manager.update_options(enabled=False, models=["another"], strategy="random")
        manager.set_active(second["id"])
        added = manager.add_relay("three", "https://three.example")
    finally:
        release.set()
    finish(task)
    settings = manager.get_settings()
    assert settings["enabled"] is False
    assert settings["models"] == ["another"]
    assert settings["strategy"] == "random"
    assert settings["active_id"] == second["id"]
    assert settings["active_url"] == second["url"]
    assert {r["id"] for r in settings["relays"]} == {first["id"], second["id"], added["id"]}
    assert settings["relays"][0]["last_probe_verified"] is True
    assert settings["relays"][0]["streaming_capability"] == "not_tested"


@pytest.mark.parametrize("replace", [False, True])
def test_probe_does_not_restore_deleted_or_replaced_relay(tmp_path, monkeypatch, replace):
    manager, first, _ = entries(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def ping(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return {"ok": False, "status": 500, "error": "No forwarding", "elapsed_ms": 4}
    monkeypatch.setattr(module, "ping_relay", ping)
    task = threaded(lambda: manager.test_relay_by_id(first["id"]))
    try:
        assert entered.wait(2)
        if replace:
            settings = manager.get_settings()
            settings["relays"][0]["url"] = "https://replacement.example"
            manager.save_settings(settings)
        else:
            manager.delete_relay(first["id"])
        expected = manager.get_settings()
    finally:
        release.set()
    finish(task)
    assert manager.get_settings() == expected
    assert manager._probe_failed == {}


def test_out_of_order_probe_cannot_override_newer_result(tmp_path, monkeypatch):
    manager, first, _ = entries(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = 0
    def ping(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(2)
            return {"ok": False, "status": 500, "error": "older", "elapsed_ms": 500}
        return good(2)
    monkeypatch.setattr(module, "ping_relay", ping)
    task = threaded(lambda: manager.test_relay_by_id(first["id"]))
    try:
        assert entered.wait(2)
        assert manager.test_relay_by_id(first["id"])["ok"]
    finally:
        release.set()
    finish(task)
    relay = manager.get_settings()["relays"][0]
    assert relay["last_status"] == 200
    assert relay["last_latency_ms"] == 2
    assert relay["last_error"] is None
    assert manager._probe_failed == {}


def test_probe_and_inference_cooldowns_are_independent(tmp_path, monkeypatch):
    manager, first, second = entries(tmp_path)
    manager.mark_relay_failed(first["url"])
    monkeypatch.setattr(module, "ping_relay", lambda *a, **kw: good())
    manager.test_relay_by_id(first["id"])
    assert first["url"] in manager._failed
    assert manager.get_relay_for_model("GLM-5.3") == second["url"]
    manager.mark_relay_ok(first["url"])
    monkeypatch.setattr(module, "ping_relay", lambda *a, **kw: {"ok": False, "status": 503})
    manager.test_relay_by_id(first["id"])
    assert first["url"] not in manager._failed
    assert first["url"] in manager._probe_failed
    manager.mark_relay_ok(first["url"])
    assert manager.get_relay_for_model("GLM-5.3") == second["url"]
    monkeypatch.setattr(module, "ping_relay", lambda *a, **kw: good())
    manager.test_relay_by_id(first["id"])
    assert manager.get_relay_for_model("GLM-5.3") == first["url"]


def test_failed_probe_save_does_not_change_cooldown(tmp_path, monkeypatch):
    manager, first, _ = entries(tmp_path)
    monkeypatch.setattr(module, "ping_relay", lambda *a, **kw: {"ok": False, "status": 503})
    def fail(*a, **kw):
        raise OSError("disk read only")
    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        manager.test_relay_by_id(first["id"])
    assert manager._probe_failed == {}
    assert manager.get_settings()["relays"][0]["last_status"] is None


@pytest.mark.parametrize("strategy", ["single", "random", "round_robin"])
@pytest.mark.parametrize("cooled", [False, True])
def test_attempted_urls_excluded_in_every_strategy_and_fallback(tmp_path, strategy, cooled):
    manager, first, second = entries(tmp_path)
    manager.update_options(strategy=strategy)
    if cooled:
        manager.mark_relay_failed(first["url"])
        manager.mark_relay_failed(second["url"])
    assert manager.get_relay_for_model("GLM-5.3", {first["url"]}) == second["url"]
    assert manager.get_relay_for_model("GLM-5.3", {first["url"], second["url"]}) is None
    assert manager.relay_required_for_model("GLM-5.3") is True
    assert manager.relay_required_for_model("GLM-5.3-Flash") is False
    manager.update_options(enabled=False)
    assert manager.relay_required_for_model("GLM-5.3") is False


def test_legacy_active_url_applies_models_and_exclusions(tmp_path):
    manager = RelayManager(tmp_path)
    assert not manager.relay_required_for_model("GLM-5.3")
    settings = manager.get_settings()
    settings["active_url"] = "https://old.example"
    manager.save_settings(settings)
    assert manager.relay_required_for_model("GLM-5.3")
    assert manager.get_relay_for_model("GLM-5.3") == settings["active_url"]
    assert manager.get_relay_for_model("GLM-5.3", {settings["active_url"]}) is None
    assert manager.get_relay_for_model("GLM-5.3-Flash") is None


def test_shutdown_probe_result_is_fenced(tmp_path, monkeypatch):
    manager, first, _ = entries(tmp_path)
    def ping(*a, **kw):
        manager.request_shutdown()
        return good()
    monkeypatch.setattr(module, "ping_relay", ping)
    before = manager.get_settings()
    manager.test_relay_by_id(first["id"])
    assert manager.get_settings() == before
    assert manager.shutdown()
