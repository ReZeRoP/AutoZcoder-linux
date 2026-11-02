"""Interrupted upstream attempts must never become partially delivered ZCode turns."""
import asyncio
import json

import httpx
import pytest

from test_stream_retry import cfg, service, REQ, run_stream
from tests_fixtures import FakeClient, FakeResponse, frame, sample_events, sample_sse
from zcode_gateway.service import UpstreamFailure


class BrokenResponse(FakeResponse):
    async def aiter_bytes(self):
        async for part in super().aiter_bytes():
            yield part
        raise httpx.RemoteProtocolError('peer closed connection without sending complete message body (incomplete chunked read)')


@pytest.mark.parametrize('cut', [2, 3, 7, 10])
@pytest.mark.parametrize('native', [False, True])
async def test_verified_discards_partial_attempt_then_delivers_one_complete_turn(service, cut, native):
    events = sample_events(tools=True)
    service.client = FakeClient(responses=[BrokenResponse(b''.join(frame(e) for e in events[:cut])),
                                          FakeResponse(sample_sse(tools=True))])
    verified = await service.verified_stream(dict(REQ), model='GLM-5.3',
                                             dialect='anthropic' if native else 'openai', anthropic=native)
    output = b''.join(verified.frames)
    assert len(service.client.calls) == 2
    assert b'stream_error' not in output and b'incomplete chunked' not in output
    if native:
        assert output.count(b'event: message_start') == 1
        assert output.count(b'event: message_stop') == 1
        assert b'opaque_signature' in output
    else:
        assert output.count(b'"role":"assistant"') == 1
        assert output.count(b'data: [DONE]') == 1
        chunks = [json.loads(line[6:]) for line in output.decode().splitlines() if line.startswith('data: {')]
        calls = [call for chunk in chunks for choice in chunk.get('choices', [])
                 for call in choice.get('delta', {}).get('tool_calls', [])]
        for index in (0, 1):
            args = ''.join(c.get('function', {}).get('arguments', '') for c in calls if c['index'] == index)
            assert json.loads(args) == {'path': 'test.txt'}
    assert all(response.closed for response in service.client.responses)
    assert (await service.pool.all())[0].in_flight == 0
    assert (await service.pool.all())[0].state.value == 'ready'


async def test_live_content_is_not_replayed_on_same_transport_cut(service):
    service.client = FakeClient(responses=[BrokenResponse(b''.join(frame(e) for e in sample_events()[:3])),
                                          FakeResponse(sample_sse())])
    _, output = await run_stream(service)
    assert len(service.client.calls) == 1
    assert b'upstream_stream_interrupted' in output
    assert b'peer closed' not in output
    assert (await service.pool.all())[0].in_flight == 0


async def test_empty_openai_block_start_does_not_commit_live_output(service):
    empty_start = b''.join(frame(e) for e in sample_events()[:2])
    service.client = FakeClient(responses=[BrokenResponse(empty_start), FakeResponse(sample_sse())])
    _, output = await run_stream(service)
    assert len(service.client.calls) == 2
    assert output.count(b'"role":"assistant"') == 1
    assert output.count(b'data: [DONE]') == 1
    assert b'upstream_stream_interrupted' not in output


async def test_untyped_business_error_is_preserved_without_retry(service):
    body = frame(sample_events()[0]) + b'data: {"error":{"code":"1305","message":"busy"}}\n\n'
    service.client = FakeClient(responses=[FakeResponse(body), FakeResponse(sample_sse())])
    with pytest.raises(UpstreamFailure) as error:
        await service.verified_stream(dict(REQ), model='GLM-5.3')
    assert error.value.code == '1305'
    assert len(service.client.calls) == 1
    assert (await service.pool.all())[0].in_flight == 0


async def test_malformed_frame_fails_before_any_verified_output(service):
    body = frame(sample_events()[0]) + b'data: {malformed\n\n'
    service.client = FakeClient(responses=[FakeResponse(body), FakeResponse(sample_sse())])
    with pytest.raises(UpstreamFailure) as error:
        await service.verified_stream(dict(REQ), model='GLM-5.3')
    assert error.value.code == 'invalid_stream'
    assert len(service.client.calls) == 1
    assert (await service.pool.all())[0].in_flight == 0


async def test_verified_retry_exhaustion_is_error_not_partial_output(service):
    service.client = FakeClient(responses=[BrokenResponse(frame(sample_events()[0])) for _ in range(3)])
    with pytest.raises(UpstreamFailure) as error:
        await service.verified_stream(dict(REQ), model='GLM-5.3')
    assert error.value.code == 'upstream_stream_interrupted'
    assert len(service.client.calls) == 3
    assert all(response.closed for response in service.client.responses)
    assert (await service.pool.all())[0].in_flight == 0


async def test_verified_limit_does_not_fall_back_to_live(service):
    service.cfg.upstream.stream_buffer_max_bytes = 4096
    events = sample_events()
    events[6]['delta']['text'] = 'x' * 6000
    service.client = FakeClient(responses=[FakeResponse(b''.join(frame(e) for e in events))])
    with pytest.raises(UpstreamFailure) as error:
        await service.verified_stream(dict(REQ), model='GLM-5.3')
    assert error.value.code == 'stream_buffer_limit_exceeded'
    assert len(service.client.calls) == 1
    assert (await service.pool.all())[0].in_flight == 0


async def test_verified_overall_deadline_closes_hung_body(service):
    class Hung(FakeResponse):
        async def aiter_bytes(self):
            yield frame(sample_events()[0])
            await asyncio.sleep(60)
    service.cfg.upstream.stream_verify_timeout_seconds = .05
    service.client = FakeClient(responses=[Hung(b'')])
    with pytest.raises(UpstreamFailure) as error:
        await service.verified_stream(dict(REQ), model='GLM-5.3')
    assert error.value.code == 'stream_buffer_timeout'
    assert service.client.responses[0].closed
    assert (await service.pool.all())[0].in_flight == 0


async def test_verified_disconnect_cancels_body_and_releases_reservation(service):
    class Hung(FakeResponse):
        async def aiter_bytes(self):
            yield frame(sample_events()[0])
            await asyncio.sleep(60)
    service.client = FakeClient(responses=[Hung(b'')])
    checks = 0
    async def disconnected():
        nonlocal checks
        checks += 1
        return checks > 2
    with pytest.raises(asyncio.CancelledError):
        await service.verified_stream(dict(REQ), model='GLM-5.3', is_disconnected=disconnected)
    assert service.client.responses[0].closed
    assert (await service.pool.all())[0].in_flight == 0
