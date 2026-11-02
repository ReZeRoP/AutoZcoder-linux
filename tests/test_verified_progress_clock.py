"""Fake time distinguishes healthy long output from endless keepalive noise."""
import pytest

from zcode_gateway.config import UpstreamConfig
from zcode_gateway.stream_delivery import RequestControl, StreamPolicyError


class Clock:
    def __init__(self):
        self.now = 0
    def __call__(self):
        return self.now


def control_with(clock, total=1800, progress=600, sends=12):
    cfg = UpstreamConfig()
    cfg.stream_verify_timeout_seconds = total
    cfg.stream_progress_timeout_seconds = progress
    cfg.stream_max_sends = sends
    return RequestControl(cfg, clock=clock)


@pytest.mark.parametrize('kind,key', [('text_delta', 'text'), ('thinking_delta', 'thinking'),
                                    ('input_json_delta', 'partial_json'), ('signature_delta', 'signature')])
def test_real_content_progress_survives_former_five_minute_cutoff(kind, key):
    clock = Clock()
    control = control_with(clock)
    for now in (200, 400, 650, 900, 1200, 1500):
        clock.now = now
        control.check()
        control.accept({'type': 'content_block_delta', 'delta': {'type': kind, key: 'x'}}, 10)
    assert control.last_progress == 1500
    assert control.events == 6
    assert control.sends == 0
    clock.now = 1800
    with pytest.raises(StreamPolicyError) as error:
        control.check()
    assert error.value.code == 'stream_buffer_timeout'
    assert error.value.details['retryable'] is False


@pytest.mark.parametrize('event', [
    {'type': 'ping'},
    {'type': 'message_start', 'message': {'id': 'synthetic'}},
    {'type': 'message_delta', 'usage': {'output_tokens': 10}},
    {'type': 'content_block_start', 'content_block': {'type': 'tool_use', 'id': 'synthetic', 'name': 'noop', 'input': {}}},
    {'type': 'content_block_delta', 'delta': {'type': 'thinking_delta', 'thinking': ''}},
    {'type': 'content_block_delta', 'delta': {'type': 'input_json_delta', 'partial_json': ''}},
])
def test_nonproductive_events_do_not_extend_progress_window(event):
    clock = Clock()
    control = control_with(clock)
    for now in (100, 200, 400, 599):
        clock.now = now
        control.accept(event, 10)
        control.check()
    assert control.last_progress == 0
    clock.now = 600
    with pytest.raises(StreamPolicyError) as error:
        control.check()
    assert error.value.code == 'stream_progress_timeout'
    assert error.value.status == 504


@pytest.mark.parametrize('block', [{'type': 'text', 'text': 'hello'},
                                   {'type': 'thinking', 'thinking': 'reasoning'},
                                   {'type': 'redacted_thinking', 'data': 'opaque'}])
def test_nonempty_content_block_start_counts_progress(block):
    clock = Clock()
    control = control_with(clock)
    clock.now = 450
    control.accept({'type': 'content_block_start', 'content_block': block}, 10)
    assert control.last_progress == 450
    clock.now = 700
    control.check()


def test_retries_and_phases_do_not_reset_request_clocks_or_send_budget():
    clock = Clock()
    control = control_with(clock, sends=2)
    initial_deadline = control.deadline
    clock.now = 100
    control.before_send()
    control.phase = 'backoff'
    clock.now = 200
    control.attempts += 1
    control.phase = 'opening'
    control.before_send()
    assert control.last_progress == 0
    assert control.deadline == initial_deadline
    with pytest.raises(StreamPolicyError) as error:
        control.before_send()
    assert error.value.code == 'stream_send_budget_exhausted'
    assert control.sends == 2


def test_byte_trickle_cannot_fake_progress_by_accounting_only():
    clock = Clock()
    control = control_with(clock)
    for now in (100, 300, 500):
        clock.now = now
        control.bytes += 1000
        control.check()
    clock.now = 600
    with pytest.raises(StreamPolicyError) as error:
        control.check()
    assert error.value.code == 'stream_progress_timeout'


def test_small_valid_payload_trickle_still_has_immutable_total_cap():
    clock = Clock()
    control = control_with(clock, total=1800, progress=600)
    for now in range(1, 1800, 100):
        clock.now = now
        control.accept({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'x'}}, 1)
        control.check()
    clock.now = 1800
    with pytest.raises(StreamPolicyError) as error:
        control.check()
    assert error.value.code == 'stream_buffer_timeout'


def test_diagnostics_only_contain_bounded_scalar_state():
    clock = Clock()
    control = control_with(clock)
    clock.now = 1
    control.accept({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'private synthetic secret'}}, 99)
    snapshot = control.diagnostics()
    assert 'private' not in str(snapshot) and 'secret' not in str(snapshot)
    assert snapshot['events'] == 1 and snapshot['bytes'] == 99
    assert all(isinstance(value, (str, int, float, bool)) for value in snapshot.values())
