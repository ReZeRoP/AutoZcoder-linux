"""Terminal opening failures have safe, bounded ledger records and diagnostics."""
import asyncio
import json
import logging
import threading
from types import SimpleNamespace

import pytest

from test_stream_retry import cfg, service, REQ
from test_relay_rate_limits import setup_client
from test_stream_routing_cleanup import TrackedBody
from zcode_gateway.service import UpstreamFailure
from zcode_gateway.upstream import RelayRoutingError


async def test_terminal_failure_records_once_without_capture_or_tokens(service, tmp_path, caplog):
    private = 'do-not-persist-this-response-secret'
    client, relay, calls = await setup_client(service.cfg, tmp_path, [(429, {
        'content-type': 'text/html; secret=' + private,
        'authorization': private, 'retry-after': private}, TrackedBody(('<html>' + private).encode()))])
    original = service.client
    service.client = client
    captured = []
    service.capture = SimpleNamespace(write=lambda **kw: captured.append(kw))
    try:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(UpstreamFailure) as failure:
                await service.verified_stream({**REQ, 'messages': [{'role': 'user', 'content': private}]}, model='GLM-5.3')
        diagnostic = failure.value.extra()['diagnostic']
        assert failure.value.code == 'relay_rate_limit_ambiguous'
        assert diagnostic['request_id'] in caplog.text
        assert diagnostic['upstream_status'] == 429
        assert diagnostic['mime'] == 'html'
        assert diagnostic['origin'] == 'unknown'
        assert private not in caplog.text and private not in json.dumps(failure.value.extra())
        rows = service.store._conn.execute('SELECT status,error_code,prompt_tokens,completion_tokens,total_tokens,duration_ms FROM usage').fetchall()
        assert len(rows) == 1
        assert rows[0][:5] == (503, 'relay_rate_limit_ambiguous', None, None, None)
        assert rows[0][5] >= 0
        assert not captured
        assert len(calls) == 1 and not relay._failed
        assert (await service.pool.all())[0].in_flight == 0
    finally:
        service.client = original
        await client.aclose()


async def test_exception_diagnostics_and_message_are_projected_not_echoed(service, caplog):
    secret = 'synthetic-secret-should-not-be-echoed'
    async def send(*args, **kwargs):
        raise RelayRoutingError('unknown-' + secret, secret, {'request_id': secret,
            'url': secret, 'body': secret, 'reason': secret, 'relay_provider': secret})
    service.client = SimpleNamespace(send=send)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(UpstreamFailure) as failure:
            await service.verified_stream(dict(REQ), model='GLM-5.3')
    assert failure.value.code == 'relay_rate_limit_ambiguous'
    assert secret not in str(failure.value)
    assert secret not in json.dumps(failure.value.extra())
    assert secret not in caplog.text
    assert len(failure.value.extra()['diagnostic']['request_id']) in (16, 32)


async def test_recording_failure_is_safe_and_does_not_replace_original_error(service, tmp_path, caplog):
    secret = 'disk-failure-secret'
    original_record = service.store.record
    def failed(**kw):
        raise OSError(secret)
    service.store.record = failed
    client, _, _ = await setup_client(service.cfg, tmp_path, [(429, {}, TrackedBody(b'unknown'))])
    original_client = service.client
    service.client = client
    try:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(UpstreamFailure) as failure:
                await service.verified_stream(dict(REQ), model='GLM-5.3')
        assert failure.value.code == 'relay_rate_limit_ambiguous'
        assert 'recording=failed' in caplog.text and secret not in caplog.text
        assert (await service.pool.all())[0].in_flight == 0
    finally:
        service.store.record = original_record
        service.client = original_client
        await client.aclose()


async def test_cancelled_observer_does_not_free_running_writer_slot(service):
    entered, release = threading.Event(), threading.Event()
    writes = []
    def write(rec, cap):
        assert cap is None
        entered.set()
        assert release.wait(5)
        writes.append(rec)
        return True
    service._write_record = write
    account = (await service.pool.all())[0]
    task = asyncio.create_task(service._record_relay_failure(account, 'key', 'openai', 'GLM-5.3',
                                                            'relay_rate_limit_ambiguous', 4))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(service._relay_record_futures) == 1
        acquired = []
        for _ in range(4):
            if service._relay_record_slots.acquire(blocking=False):
                acquired.append(True)
        assert len(acquired) == 3
        for _ in acquired:
            service._relay_record_slots.release()
    finally:
        release.set()
        for _ in range(100):
            if not service._relay_record_futures:
                break
            await asyncio.sleep(.01)
    assert len(writes) == 1 and not service._relay_record_futures
    assert writes[0]['total_tokens'] is None


async def test_saturated_writer_does_not_queue_another_task(service):
    for _ in range(4):
        assert service._relay_record_slots.acquire(blocking=False)
    try:
        account = (await service.pool.all())[0]
        result = await service._record_relay_failure(account, 'key', 'openai', 'GLM-5.3',
                                                     'relay_rate_limit_ambiguous', 4)
        assert result == 'saturated'
        assert not service._relay_record_futures
    finally:
        for _ in range(4):
            service._relay_record_slots.release()
