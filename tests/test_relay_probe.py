"""Offline forwarding challenges: bounded reads, deadlines, TLS and privacy."""
import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from zcode_gateway import relay_deploy as rd


class Raw(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.bytes_read = 0
        self.read_calls = 0

    def read1(self, size, decode_content=False):
        assert decode_content is False
        self.read_calls += 1
        chunk = super().read1(size)
        self.bytes_read += len(chunk)
        return chunk


class Response:
    def __init__(self, body=b"", status=200, headers=None):
        self.status_code = status
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.raw = Raw(body)
        self.closed = False

    @property
    def text(self):
        raise AssertionError("Never buffer or expose a raw provider body")

    def json(self):
        raise AssertionError("Only decode bounded bytes")

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in probe tests")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def payload(kwargs):
    path = kwargs["headers"]["x-relay-path"]
    nonce = parse_qs(urlsplit(path).query)["autozcoder_probe"][0]
    return {"args": {"autozcoder_probe": nonce}, "url": "https://httpbin.org" + path}


def install_echo(monkeypatch, transform=lambda data: data, **response_kwargs):
    seen = []

    def get(url, **kwargs):
        data = transform(payload(kwargs))
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
        response = Response(body, **response_kwargs)
        seen.append((kwargs, response))
        return response

    monkeypatch.setattr(rd.requests, "get", get)
    return seen


def test_unique_nonce_verified_with_finite_tls_streamed_request(monkeypatch):
    seen = install_echo(monkeypatch)
    for _ in range(2):
        result = rd.ping_relay("https://relay.example", timeout=1000)
        assert result["ok"] and result["probe_verified"]
        assert result["probe_kind"] == "forwarding" and result["error_code"] == ""
        assert result["model_capability"] == result["streaming_capability"] == "not_tested"
    assert seen[0][0]["headers"]["x-relay-path"] != seen[1][0]["headers"]["x-relay-path"]
    for kwargs, response in seen:
        assert kwargs["verify"] is True and kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True and 0 < kwargs["timeout"].total <= 12
        assert kwargs["headers"]["Accept-Encoding"] == "identity"
        assert response.closed


@pytest.mark.parametrize("transform", [
    lambda d: b"<html>ok secret-token</html>",
    lambda d: b'{"broken":',
    lambda d: {"args": {"autozcoder_probe": "old-cached"}, "url": d["url"]},
    lambda d: {"args": d["args"], "url": d["url"].replace("httpbin.org", "evil.example")},
    lambda d: {"args": d["args"], "url": d["url"].replace("https:", "http:")},
    lambda d: {"args": d["args"], "url": d["url"].replace("/get?", "/anything?")},
    lambda d: {"args": d["args"], "url": d["url"] + "&extra=1"},
    lambda d: {"args": d["args"], "url": d["url"].replace("httpbin.org", "user:secret@httpbin.org")},
    lambda d: {"args": d["args"], "url": d["url"] + "#secret"},
    lambda d: {"args": d["args"], "url": None},
    lambda d: [],
])
def test_rejects_static_malformed_or_wrong_challenge(monkeypatch, transform):
    seen = install_echo(monkeypatch, transform)
    result = rd.ping_relay("https://relay.example")
    assert result["ok"] is False and result["probe_verified"] is False
    assert result["error_code"] in ("probe_invalid_json", "probe_mismatch")
    assert "secret" not in json.dumps(result)
    assert seen[0][1].closed


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 404, 429, 500, 502])
def test_non200_never_reads_or_leaks_body(monkeypatch, status):
    response = Response(b"secret-access-token", status=status)
    monkeypatch.setattr(rd.requests, "get", lambda *a, **k: response)
    result = rd.ping_relay("https://relay.example")
    assert result["status"] == status and result["ok"] is False
    assert "secret" not in json.dumps(result)
    assert response.closed and response.raw.read_calls == 0
    if status == 429:
        assert "origin is not verified" in result["error"]


@pytest.mark.parametrize("advertised", [False, True])
def test_oversized_response_bounded_to_one_extra_byte(monkeypatch, advertised):
    response = Response(b"x" * (rd.PROBE_MAX_BYTES * 2), headers=(
        {"content-length": str(rd.PROBE_MAX_BYTES + 1)} if advertised else {}))
    monkeypatch.setattr(rd.requests, "get", lambda *a, **k: response)
    result = rd.ping_relay("https://relay.example")
    assert result["error_code"] == "probe_too_large"
    assert response.raw.bytes_read <= rd.PROBE_MAX_BYTES + 1
    assert response.closed


def test_exact_size_limit_accepts_valid_json(monkeypatch):
    def padded(data):
        body = json.dumps(data).encode()
        return body + b" " * (rd.PROBE_MAX_BYTES - len(body))
    seen = install_echo(monkeypatch, padded)
    assert rd.ping_relay("https://relay.example")["ok"] is True
    assert seen[0][1].raw.bytes_read == rd.PROBE_MAX_BYTES


@pytest.mark.parametrize("headers,code", [
    ({"content-encoding": "gzip"}, "probe_encoded_body"),
    ({"content-type": "text/html"}, "probe_invalid_json"),
    ({"content-length": "invalid"}, "probe_invalid_json"),
])
def test_rejects_unsafe_or_unexpected_body_metadata(monkeypatch, headers, code):
    seen = install_echo(monkeypatch, headers=headers)
    result = rd.ping_relay("https://relay.example")
    assert result["error_code"] == code
    assert seen[0][1].closed and seen[0][1].raw.bytes_read == 0


@pytest.mark.parametrize("url", ["", "garbage", "http://relay.example", "https://", "https://a:secret@relay.example", "https://relay.example:99999", "https://relay.example/#fragment"])
def test_invalid_urls_no_external_request(monkeypatch, url):
    monkeypatch.setattr(rd.requests, "get", lambda *a, **k: pytest.fail("invalid URL reached HTTP"))
    result = rd.ping_relay(url)
    assert result["error_code"] == "invalid_relay_url" and result["status"] == 400
    assert "elapsed_ms" in result


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), None])
def test_invalid_timeouts_no_external_request(monkeypatch, timeout):
    monkeypatch.setattr(rd.requests, "get", lambda *a, **k: pytest.fail("invalid timeout reached HTTP"))
    assert rd.ping_relay("https://relay.example", timeout)["error_code"] == "invalid_probe_timeout"


@pytest.mark.parametrize("exc,code", [
    (requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED secret"), "probe_tls_error"),
    (requests.exceptions.ReadTimeout("secret-token"), "probe_timeout"),
    (requests.exceptions.ConnectionError("secret-token"), "probe_network_error"),
])
def test_network_error_codes_never_include_exception(monkeypatch, exc, code):
    def get(*args, **kwargs):
        raise exc
    monkeypatch.setattr(rd.requests, "get", get)
    result = rd.ping_relay("https://relay.example")
    assert result["error_code"] == code and "secret" not in json.dumps(result)


def test_total_deadline_includes_headers_and_slow_body(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(rd.time, "monotonic", lambda: clock[0])
    response = Response(b" " * 100)
    reads = []
    original = response.raw.read1

    def slow_read(size, decode_content=False):
        clock[0] += 4
        reads.append(size)
        return original(1, decode_content)

    response.raw.read1 = slow_read

    def get(*args, **kwargs):
        clock[0] += 5
        return response

    monkeypatch.setattr(rd.requests, "get", get)
    result = rd.ping_relay("https://relay.example")
    assert result["error_code"] == "probe_timeout" and len(reads) == 2
    assert response.closed


def test_body_reader_resets_socket_timeout_to_remaining(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(rd.time, "monotonic", lambda: clock[0])
    seen = install_echo(monkeypatch)
    socket_timeouts = []
    original_get = rd.requests.get

    def get(*args, **kwargs):
        response = original_get(*args, **kwargs)
        sock = type("Socket", (), {"settimeout": lambda self, v: socket_timeouts.append(v)})()
        response.raw._connection = type("Connection", (), {"sock": sock})()
        original = response.raw.read1
        def read(size, decode_content=False):
            clock[0] += 1
            return original(size, decode_content)
        response.raw.read1 = read
        return response

    monkeypatch.setattr(rd.requests, "get", get)
    assert rd.ping_relay("https://relay.example")["ok"] is True
    assert socket_timeouts == [12, 11]
    assert seen[0][1].closed
