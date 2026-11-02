"""Purely stubbed provider calls and deterministic deployment lifecycle checks."""
import json
import threading

import pytest
import requests

from zcode_gateway import relay_deploy as rd


class Response:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.data = data if data is not None else {}
        self.closed = False

    @property
    def text(self):
        raise AssertionError("Provider error bodies must not be read")

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 0.0
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        return self.stopped


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in deployment tests")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(rd.time, "monotonic", lambda: value.now)
    return value


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_control_rejects_nonfinite_budget(timeout):
    with pytest.raises(ValueError):
        rd.DeployControl(timeout)


def test_monotonic_total_budget_clamps_wait_and_fences_timeout(clock, monkeypatch):
    monkeypatch.setattr(rd.time, "time", lambda: 10**20)
    control = rd.DeployControl(timeout=2, stop_event=clock)
    control.wait(1.5)
    assert control.remaining() == .5
    with pytest.raises(rd.RelayDeployError) as error:
        control.wait(99)
    assert error.value.code == "deployment_timeout" and not error.value.outcome_unknown
    assert clock.waits == [1.5, .5] and control.remaining() == 0
    assert rd.DeployControl(timeout=999).remaining() == 300


def test_stop_interrupts_real_wait_without_sleeping():
    entered = threading.Event()
    stop = threading.Event()
    control = rd.DeployControl(stop_event=stop)
    errors = []

    def worker():
        entered.set()
        try:
            control.wait(250)
        except rd.RelayDeployError as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(1)
    stop.set()
    thread.join(1)
    assert not thread.is_alive()
    assert errors[0].code == "deployment_stopped"


def test_checkpoint_keeps_late_ack_and_filters_unsafe_fields(clock):
    phases = []
    control = rd.DeployControl(stop_event=clock, on_phase=lambda phase, **kw: phases.append((phase, kw)))
    control.mutation_attempted = True
    clock.set()
    with pytest.raises(rd.RelayDeployError) as error:
        control.checkpoint("remote_created", deployment_id="dpl_123", token="secret",
                           url="https://relay.example/path?token=secret#fragment", error="secret",
                           name="Bearer secret")
    assert error.value.code == "deployment_stopped" and error.value.outcome_unknown
    assert error.value.remote == {"deployment_id": "dpl_123", "url": "https://relay.example/path"}
    assert phases == [("remote_created", error.value.remote)]
    error.value.remote["deployment_id"] = "modified"
    assert control.remote["deployment_id"] == "dpl_123"


@pytest.mark.parametrize("verb", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize("failure", [
    requests.exceptions.ReadTimeout("secret-token"),
    requests.exceptions.ConnectTimeout("secret-token"),
    requests.exceptions.SSLError("EOF secret-token"),
    requests.exceptions.ConnectionError("secret-token"),
    500, 502, 503, 504, 307,
])
def test_ambiguous_mutations_never_replay_and_fence_all_later_calls(monkeypatch, clock, verb, failure):
    control = rd.DeployControl(stop_event=clock)
    calls = []
    response = None
    if isinstance(failure, int):
        response = Response(failure)

    def call(*args, **kwargs):
        calls.append(kwargs)
        if response:
            return response
        raise failure

    monkeypatch.setattr(rd.requests, verb, call)
    with pytest.raises(rd.RelayDeployError) as error:
        rd._call(verb, "https://api.example/resources", control=control)
    assert len(calls) == 1 and clock.waits == []
    assert error.value.code == "remote_outcome_unknown" and error.value.outcome_unknown
    assert "secret" not in error.value.user_message and "secret" not in error.value.detail
    with pytest.raises(rd.RelayDeployError, match="outcome is unknown"):
        rd._call(verb, "https://api.example/resources", control=control)
    assert len(calls) == 1 and control.mutation_attempted
    if response:
        assert response.closed


def test_readonly_retries_close_responses_and_shrink_budget(monkeypatch, clock):
    control = rd.DeployControl(timeout=20, stop_event=clock)
    first, final = Response(502), Response(200)
    seq = [requests.exceptions.ConnectionError("connection reset"), first, final]
    timeouts = []

    def get(*args, **kwargs):
        timeouts.append(kwargs["timeout"].total)
        assert kwargs["verify"] is True and kwargs["allow_redirects"] is False
        clock.now += 2
        value = seq.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(rd.requests, "get", get)
    assert rd._call("get", "https://api.example/status", timeout=45, control=control) is final
    assert timeouts == [20, 16.5, 11.5]
    assert clock.waits == [1.5, 3]
    assert first.closed and final.closed and not control.mutation_attempted


def test_stop_during_retry_prevents_next_request(monkeypatch, clock):
    calls = []
    def get(*args, **kwargs):
        calls.append(1)
        clock.set()
        raise requests.exceptions.ConnectionError("connection reset")
    monkeypatch.setattr(rd.requests, "get", get)
    with pytest.raises(rd.RelayDeployError) as error:
        rd._call("get", "https://api.example/status", control=rd.DeployControl(stop_event=clock))
    assert error.value.code == "deployment_stopped" and calls == [1]


def test_vercel_ready_uses_one_post_and_checkpoints_remote_ids(monkeypatch, clock):
    phases = []
    control = rd.DeployControl(stop_event=clock, on_phase=lambda p, **kw: phases.append((p, kw)))
    posts = []
    monkeypatch.setattr(rd.requests, "post", lambda *a, **k: (
        posts.append(k) or Response(data={"id": "dpl_1", "projectId": "prj_1", "url": "relay.vercel.app"})))
    monkeypatch.setattr(rd.requests, "patch", lambda *a, **k: Response())
    seq = [requests.exceptions.ReadTimeout("secret"), Response(data={"readyState": "BUILDING"}),
           Response(data={"readyState": "READY", "url": "relay.vercel.app"})]
    def get(*args, **kwargs):
        value = seq.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(rd.requests, "get", get)
    result = rd.deploy_vercel("secret-token", "relay", control=control)
    assert result["url"] == "https://relay.vercel.app" and len(posts) == 1
    assert control.remote["deployment_id"] == "dpl_1" and control.remote["project_id"] == "prj_1"
    assert [p for p, kw in phases] == ["creating", "remote_created", "configuring", "waiting_ready", "ready"]
    assert "secret" not in json.dumps(phases)


@pytest.mark.parametrize("stop_kind", ["stop", "timeout"])
def test_late_creation_ack_is_recorded_before_stop_or_deadline(monkeypatch, clock, stop_kind):
    control = rd.DeployControl(timeout=5, stop_event=clock)
    def post(*args, **kwargs):
        if stop_kind == "stop":
            clock.set()
        else:
            clock.now = 6
        return Response(data={"id": "dpl_1", "projectId": "prj_1", "url": "relay.vercel.app"})
    monkeypatch.setattr(rd.requests, "post", post)
    monkeypatch.setattr(rd.requests, "patch", lambda *a, **k: pytest.fail("mutation after stop"))
    with pytest.raises(rd.RelayDeployError) as error:
        rd.deploy_vercel("token", "relay", control=control)
    assert error.value.code == ("deployment_stopped" if stop_kind == "stop" else "deployment_timeout")
    assert error.value.remote["deployment_id"] == "dpl_1" and error.value.outcome_unknown


@pytest.mark.parametrize("failure", [503, requests.exceptions.ReadTimeout("secret")])
def test_deno_failed_deploy_retains_app_and_never_deletes(monkeypatch, clock, failure):
    control = rd.DeployControl(stop_event=clock)
    calls = []
    def post(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return Response(data={"id": "app_123"})
        if isinstance(failure, Exception):
            raise failure
        return Response(failure)
    monkeypatch.setattr(rd.requests, "post", post)
    monkeypatch.setattr(rd.requests, "delete", lambda *a, **k: pytest.fail("ambiguous cleanup"))
    with pytest.raises(rd.RelayDeployError) as error:
        rd.deploy_deno("token", "org.deno.net", "relay", control=control)
    assert error.value.outcome_unknown and error.value.remote["app_id"] == "app_123"
    assert len(calls) == 2


def test_deno_readiness_monotonic_timeout_has_no_cleanup(monkeypatch, clock):
    control = rd.DeployControl(stop_event=clock)
    seq = [Response(data={"id": "app_1"}), Response(data={"id": "rev_1"})]
    monkeypatch.setattr(rd.requests, "post", lambda *a, **k: seq.pop(0))
    monkeypatch.setattr(rd.requests, "get", lambda *a, **k: Response(data={"status": "building"}))
    monkeypatch.setattr(rd.requests, "delete", lambda *a, **k: pytest.fail("timed out cleanup"))
    with pytest.raises(rd.RelayDeployError) as error:
        rd.deploy_deno("token", "org.deno.net", "relay", control=control)
    assert error.value.code == "deployment_not_ready" and error.value.outcome_unknown
    assert clock.now == 60 and control.remote["revision_id"] == "rev_1"
    with pytest.raises(rd.RelayDeployError):
        control.check()


def test_deno_ready_and_failure_are_distinct(monkeypatch, clock):
    for state in ("succeeded", "failed"):
        seq = [Response(data={"id": "app_1"}), Response(data={"id": "rev_1"})]
        monkeypatch.setattr(rd.requests, "post", lambda *a, **k: seq.pop(0))
        monkeypatch.setattr(rd.requests, "get", lambda *a, **k: Response(data={"status": state}))
        control = rd.DeployControl(stop_event=clock)
        if state == "succeeded":
            assert rd.deploy_deno("token", "org.deno.net", "relay", control=control)["ok"]
        else:
            with pytest.raises(rd.RelayDeployError) as error:
                rd.deploy_deno("token", "org.deno.net", "relay", control=control)
            assert error.value.code == "deployment_failed" and not error.value.outcome_unknown
            assert error.value.remote["revision_id"] == "rev_1"


@pytest.mark.parametrize("bad_data", [[], {}, {"id": "bad/path"}, ValueError("secret JSON body")])
def test_invalid_creation_response_never_replays(monkeypatch, clock, bad_data):
    calls = []
    monkeypatch.setattr(rd.requests, "post", lambda *a, **k: calls.append(1) or Response(data=bad_data))
    control = rd.DeployControl(stop_event=clock)
    with pytest.raises(rd.RelayDeployError) as error:
        rd.deploy_vercel("token", "relay", control=control)
    assert error.value.code == "provider_response_invalid" and error.value.outcome_unknown
    assert calls == [1]
    with pytest.raises(rd.RelayDeployError):
        control.check()


def test_valid_remote_id_survives_invalid_sibling_url(monkeypatch, clock):
    monkeypatch.setattr(rd.requests, "post", lambda *a, **k: Response(data={
        "id": "dpl_1", "projectId": "prj_1", "url": "user:secret@relay.vercel.app",
    }))
    control = rd.DeployControl(stop_event=clock)
    with pytest.raises(rd.RelayDeployError) as error:
        rd.deploy_vercel("token", "relay", control=control)
    assert error.value.code == "provider_response_invalid" and error.value.outcome_unknown
    assert error.value.remote["deployment_id"] == "dpl_1" and "url" not in error.value.remote


def test_provider_api_errors_have_safe_legacy_message_and_job_code(monkeypatch, clock):
    monkeypatch.setattr(rd.requests, "post", lambda *a, **k: Response(401))
    with pytest.raises(rd.RelayDeployError) as error:
        rd.deploy_vercel("secret-token", "relay", control=rd.DeployControl(stop_event=clock))
    assert error.value.code == "provider_auth" and not error.value.outcome_unknown
    assert "Vercel rejected the API token" in error.value.user_message
    assert "secret" not in json.dumps(vars(error.value))


@pytest.mark.parametrize("token", ["", "secret-token"])
def test_netlify_unsupported_before_any_external_calls(monkeypatch, token):
    for method in ("post", "get", "put", "patch", "delete"):
        monkeypatch.setattr(rd.requests, method, lambda *a, **k: pytest.fail("Netlify made external call"))
    control = rd.DeployControl()
    with pytest.raises(rd.RelayDeployError) as error:
        rd.deploy_netlify(token, "relay", control=control)
    assert error.value.code == "netlify_streaming_unsupported"
    assert "buffers responses" in error.value.user_message
    assert not control.mutation_attempted and not error.value.outcome_unknown
