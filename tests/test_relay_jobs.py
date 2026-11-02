"""Owned deployment jobs use gated fake providers and temporary state only."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import pytest

from zcode_gateway import relay_jobs, relay_manager
from zcode_gateway.relay_deploy import RelayDeployError
from zcode_gateway.relay_manager import DeploymentJobError, RelayManager

PARAMS = {"vercelToken": "private-test-token", "projectName": "synthetic-relay"}
RESULT = {"ok": True, "name": "synthetic-relay", "url": "https://synthetic.example", "type": "vercel"}


@pytest.fixture(autouse=True)
def forbid_live_providers(monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("Install a fake provider in every job test")
    for provider in ("vercel", "cloudflare", "deno", "netlify"):
        monkeypatch.setattr(relay_manager, "deploy_" + provider, forbidden)


def operation_id(manager):
    return f"{manager.deployment_snapshot()['generation']}:{uuid.uuid4()}"


def wait_finished(manager, identifier, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = manager.deployment_snapshot(identifier)
        if snapshot["active_id"] is None:
            assert snapshot["job"]["status"] != "running"
            return snapshot
        time.sleep(.005)
    pytest.fail("fake deployment thread did not exit")


def gated_provider(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls, controls = [], []
    def deploy(**kwargs):
        calls.append(kwargs)
        control = kwargs["control"]
        controls.append(control)
        control.mutation_attempted = True
        control.checkpoint("remote_created", deployment_id="remote-123", provider="vercel")
        entered.set()
        assert release.wait(10)
        return dict(RESULT)
    monkeypatch.setattr(relay_manager, "deploy_vercel", deploy)
    return entered, release, calls, controls


def test_start_is_prompt_duplicate_and_lost_ack_never_repeat(monkeypatch, tmp_path):
    manager = RelayManager(tmp_path)
    entered, release, calls, _ = gated_provider(monkeypatch)
    identifier = operation_id(manager)
    try:
        started = time.monotonic()
        snapshot = manager.start_deployment("vercel", PARAMS, identifier)
        assert time.monotonic() - started < .5
        assert snapshot["job"]["status"] == "running"
        assert entered.wait(2)
        observed = manager.deployment_snapshot(identifier)
        assert observed["job"]["remote"]["deployment_id"] == "remote-123"
        duplicate = manager.start_deployment("vercel", dict(PARAMS), identifier)
        assert duplicate["job"]["id"] == identifier
        assert len(calls) == 1
        with pytest.raises(DeploymentJobError) as exc:
            manager.start_deployment("vercel", dict(PARAMS, vercelToken="new-token"), identifier)
        assert (exc.value.status, exc.value.code) == (409, "operation_payload_conflict")
        with pytest.raises(DeploymentJobError) as exc:
            manager.start_deployment("vercel", PARAMS, operation_id(manager))
        assert exc.value.status == 409
        assert exc.value.active_id == identifier
        duplicate["job"]["remote"].clear()
        assert manager.deployment_snapshot(identifier)["job"]["remote"]
    finally:
        release.set()
    finished = wait_finished(manager, identifier)
    assert finished["job"]["status"] == "succeeded"
    assert manager.start_deployment("vercel", PARAMS, identifier)["job"] == finished["job"]
    assert len(calls) == 1
    assert manager.get_relay_count() == 1
    assert PARAMS["vercelToken"] not in json.dumps(finished)
    assert PARAMS["vercelToken"] not in manager.path.read_text()


def test_slow_worker_does_not_inherit_browser_five_second_timeout(monkeypatch, tmp_path):
    manager = RelayManager(tmp_path)
    entered, release, calls, _ = gated_provider(monkeypatch)
    identifier = operation_id(manager)
    try:
        manager.start_deployment("vercel", PARAMS, identifier)
        assert entered.wait(2)
        # This one regression deliberately exceeds the old dashboard timeout.
        assert not release.wait(5.05)
        assert manager.deployment_snapshot(identifier)["job"]["status"] == "running"
        assert len(calls) == 1
    finally:
        release.set()
    assert wait_finished(manager, identifier)["job"]["status"] == "succeeded"


def test_simultaneous_submit_has_exactly_one_owned_worker(monkeypatch, tmp_path):
    manager = RelayManager(tmp_path)
    entered, release, calls, _ = gated_provider(monkeypatch)
    barrier = threading.Barrier(3)
    results, errors = [], []
    ids = [operation_id(manager), operation_id(manager)]
    def submit(identifier):
        barrier.wait()
        try:
            results.append(manager.start_deployment("vercel", PARAMS, identifier))
        except DeploymentJobError as exc:
            errors.append(exc)
    threads = [threading.Thread(target=submit, args=(identifier,)) for identifier in ids]
    try:
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
        assert entered.wait(2)
        assert len(results) == 1
        assert len(errors) == 1 and errors[0].status == 409
        assert len(calls) == 1
    finally:
        release.set()
    wait_finished(manager, results[0]["job"]["id"])


@pytest.mark.parametrize("provider,params", [
    ("netlify", {"token": "private"}), ("custom", {"token": "private"}),
    ("vercel", {}), ("cloudflare", {"apiToken": "private"}),
    ("deno", {"denoToken": "private"}),
    ("vercel", dict(PARAMS, projectName="../../unsafe")),
])
def test_validate_and_gate_before_admission(monkeypatch, tmp_path, provider, params):
    manager = RelayManager(tmp_path)
    with pytest.raises(DeploymentJobError) as exc:
        manager.start_deployment(provider, params, operation_id(manager))
    assert exc.value.status == 400
    assert manager.deployment_snapshot()["job"] is None
    assert manager._deployments._thread is None
    assert not manager.path.exists()
    with pytest.raises(DeploymentJobError):
        manager.submit_legacy_deployment(provider, params)
    assert not manager._deployments._records


def test_generation_restart_and_unknown_ids_fail_closed(monkeypatch, tmp_path):
    manager = RelayManager(tmp_path)
    unknown = operation_id(manager)
    with pytest.raises(DeploymentJobError) as exc:
        manager.deployment_snapshot(unknown)
    assert exc.value.status == 404
    fresh = RelayManager(tmp_path)
    with pytest.raises(DeploymentJobError) as exc:
        fresh.deployment_snapshot(unknown)
    assert exc.value.status == 410
    with pytest.raises(DeploymentJobError) as exc:
        fresh.start_deployment("vercel", PARAMS, unknown)
    assert exc.value.status == 410
    with pytest.raises(DeploymentJobError) as exc:
        fresh.start_deployment("vercel", PARAMS, "bad-id")
    assert exc.value.status == 400


def test_terminal_retention_rotates_admission_without_replaying_evicted_id(monkeypatch, tmp_path):
    manager = RelayManager(tmp_path)
    calls = []
    def deploy(**kwargs):
        calls.append(True)
        return dict(RESULT)
    monkeypatch.setattr(relay_manager, "deploy_vercel", deploy)
    original_generation = manager.deployment_snapshot()["generation"]
    identifiers = []
    for _ in range(relay_jobs.MAX_TERMINAL_JOBS + 1):
        identifier = operation_id(manager)
        identifiers.append(identifier)
        manager.start_deployment("vercel", PARAMS, identifier)
        wait_finished(manager, identifier)
    assert len(manager._deployments._records) == 32
    assert manager.deployment_snapshot()["generation"] != original_generation
    assert manager.deployment_snapshot(identifiers[-2])["job"]["status"] == "succeeded"
    with pytest.raises(DeploymentJobError) as exc:
        manager.start_deployment("vercel", PARAMS, identifiers[0])
    assert exc.value.status == 410
    with pytest.raises(DeploymentJobError) as exc:
        manager.deployment_snapshot(identifiers[0])
    assert exc.value.status == 410
    assert len(calls) == 33


def test_registration_failure_retries_only_local_write(monkeypatch, tmp_path):
    manager = RelayManager(tmp_path)
    calls = []
    def deploy(**kwargs):
        calls.append(True)
        return dict(RESULT)
    monkeypatch.setattr(relay_manager, "deploy_vercel", deploy)
    original = Path.replace
    def fail(*args, **kwargs):
        raise OSError("disk full private-test-token")
    monkeypatch.setattr(Path, "replace", fail)
    identifier = operation_id(manager)
    manager.start_deployment("vercel", PARAMS, identifier)
    failed = wait_finished(manager, identifier)
    assert failed["job"]["status"] == "registration_failed"
    assert failed["job"]["remote"]["url"] == RESULT["url"]
    assert manager.get_relay_count() == 0
    assert PARAMS["vercelToken"] not in json.dumps(failed)
    assert manager.start_deployment("vercel", PARAMS, identifier)["job"]["status"] == "registration_failed"
    assert len(calls) == 1
    monkeypatch.setattr(Path, "replace", original)
    manager.retry_deployment_registration(identifier)
    finished = wait_finished(manager, identifier)
    assert finished["job"]["status"] == "succeeded"
    assert manager.get_relay_count() == 1
    assert len(calls) == 1
    manager.retry_deployment_registration(identifier)
    assert manager.get_relay_count() == 1


def test_remote_ambiguity_is_not_retried_and_secrets_never_survive(monkeypatch, tmp_path):
    manager = RelayManager(tmp_path)
    calls = []
    def deploy(**kwargs):
        calls.append(True)
        raise RelayDeployError("token private-test-token raw response", detail="Bearer private-test-token",
                               code="remote_outcome_unknown", outcome_unknown=True,
                               remote={"deployment_id": "deployment-42", "name": "private-test-token",
                                       "url": "https://user:private-test-token@example.test/?token=private-test-token",
                                       "api_token": "private-test-token"})
    monkeypatch.setattr(relay_manager, "deploy_vercel", deploy)
    identifier = operation_id(manager)
    manager.start_deployment("vercel", PARAMS, identifier)
    snapshot = wait_finished(manager, identifier)
    assert snapshot["job"]["status"] == "outcome_unknown"
    assert snapshot["job"]["remote"] == {"deployment_id": "deployment-42"}
    assert PARAMS["vercelToken"] not in json.dumps(snapshot)
    assert PARAMS["vercelToken"] not in repr(manager._deployments._records)
    assert not manager.path.exists()
    manager.start_deployment("vercel", PARAMS, identifier)
    assert calls == [True]
    with pytest.raises(DeploymentJobError) as exc:
        manager.retry_deployment_registration(identifier)
    assert exc.value.status == 409


@pytest.mark.parametrize("exception", [
    RuntimeError("https://user:private-test-token@provider/private-test-token"),
    RelayDeployError("provider body private-test-token", code="provider_auth"),
])
def test_exception_text_never_enters_public_status(monkeypatch, tmp_path, exception):
    manager = RelayManager(tmp_path)
    def deploy(**kwargs):
        raise exception
    monkeypatch.setattr(relay_manager, "deploy_vercel", deploy)
    identifier = operation_id(manager)
    manager.start_deployment("vercel", PARAMS, identifier)
    snapshot = wait_finished(manager, identifier)
    assert snapshot["job"]["status"] == "failed"
    assert "private-test-token" not in json.dumps(snapshot)
    assert snapshot["job"]["error"]["code"] in ("provider_auth", "deployment_failed")


def test_timeout_observer_cannot_release_resistant_worker_or_register_late(monkeypatch, tmp_path):
    manager = RelayManager(tmp_path)
    entered, release, calls, controls = gated_provider(monkeypatch)
    identifier = operation_id(manager)
    try:
        manager.start_deployment("vercel", PARAMS, identifier)
        assert entered.wait(2)
        controls[0].deadline = time.monotonic() - 1
        snapshot = manager.deployment_snapshot(identifier)
        assert snapshot["job"]["status"] == "outcome_unknown"
        assert snapshot["job"]["error"]["code"] == "deployment_timeout"
        assert snapshot["active_id"] == identifier
        assert snapshot["job"]["finished_at"] is None
        with pytest.raises(DeploymentJobError) as exc:
            manager.start_deployment("vercel", PARAMS, operation_id(manager))
        assert exc.value.status == 409
        assert len(calls) == 1
        assert manager._deployments._thread.is_alive()
    finally:
        release.set()
    finished = wait_finished(manager, identifier)
    assert finished["job"]["status"] == "outcome_unknown"
    assert manager.get_relay_count() == 0
    assert not manager.path.exists()


def test_shutdown_keeps_resistant_worker_owned_and_fences_local_commit(monkeypatch, tmp_path):
    manager = RelayManager(tmp_path)
    entered, release, _, _ = gated_provider(monkeypatch)
    identifier = operation_id(manager)
    try:
        manager.start_deployment("vercel", PARAMS, identifier)
        assert entered.wait(2)
        manager.request_shutdown()
        with pytest.raises(TimeoutError):
            manager.shutdown(deadline=time.monotonic() + .01)
        snapshot = manager.deployment_snapshot(identifier)
        assert snapshot["active_id"] == identifier
        assert snapshot["job"]["status"] == "outcome_unknown"
        with pytest.raises(DeploymentJobError) as exc:
            manager.start_deployment("vercel", PARAMS, operation_id(manager))
        assert exc.value.status == 503
    finally:
        release.set()
    wait_finished(manager, identifier)
    assert manager.shutdown()
    assert manager.get_relay_count() == 0
    assert not manager.path.exists()


def test_stop_during_disk_write_is_fenced_before_replace(monkeypatch, tmp_path):
    manager = RelayManager(tmp_path)
    monkeypatch.setattr(relay_manager, "deploy_vercel", lambda **kw: dict(RESULT))
    original = Path.write_text
    def stop_during_write(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.suffix == ".tmp":
            manager.request_shutdown()
        return result
    monkeypatch.setattr(Path, "write_text", stop_during_write)
    identifier = operation_id(manager)
    manager.start_deployment("vercel", PARAMS, identifier)
    finished = wait_finished(manager, identifier)
    assert finished["job"]["status"] == "outcome_unknown"
    assert manager.get_relay_count() == 0
    assert not manager.path.exists()
    assert not manager.path.with_suffix(".tmp").exists()


def test_payload_cleared_and_control_detached_after_worker_exit(monkeypatch, tmp_path):
    manager = RelayManager(tmp_path)
    held = []
    def invoke(provider, payload, control):
        held.append((payload, control))
        return dict(RESULT)
    manager._deployments._deploy = invoke
    identifier = operation_id(manager)
    manager.start_deployment("vercel", PARAMS, identifier)
    wait_finished(manager, identifier)
    assert held[0][0] == {}
    assert held[0][1].remote == {}
    assert manager._deployments._thread is None
    assert manager._deployments._control is None
    assert "private-test-token" not in repr(manager._deployments._records)
    assert "private-test-token" not in manager.path.read_text()


def test_legacy_uses_same_admission_and_optional_operation_key_is_ignored(monkeypatch, tmp_path):
    manager = RelayManager(tmp_path)
    entered, release, calls, _ = gated_provider(monkeypatch)
    try:
        snapshot = manager.submit_legacy_deployment("vercel", PARAMS)
        identifier = snapshot["job"]["id"]
        assert entered.wait(2)
        assert manager.start_deployment("vercel", dict(PARAMS, operation_id=identifier), identifier)["job"]["id"] == identifier
        with pytest.raises(DeploymentJobError) as exc:
            manager.submit_legacy_deployment("vercel", PARAMS)
        assert exc.value.status == 409
    finally:
        release.set()
    wait_finished(manager, identifier)
    assert len(calls) == 1


def test_legacy_synchronous_success_shape_is_preserved(monkeypatch, tmp_path):
    manager = RelayManager(tmp_path)
    monkeypatch.setattr(relay_manager, "deploy_vercel", lambda **kw: dict(RESULT))
    result = manager.deploy_and_add("vercel", PARAMS)
    assert result["ok"] is True
    assert result["relay"]["url"] == RESULT["url"]
    assert manager.deployment_snapshot()["job"]["status"] == "succeeded"
