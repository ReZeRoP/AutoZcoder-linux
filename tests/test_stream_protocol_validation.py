"""Synthetic malformed streams must fail explicitly, never silently lose events."""
import json

import pytest

from zcode_gateway.upstream import iter_sse_events
from zcode_gateway.zcode_protocol import MessageAccumulator, OpenAITranslator, validate_stream_event
from tests_fixtures import sample_events


@pytest.mark.parametrize('data', ['{bad', '[]', 'null', '"text"', '[DONE]'])
def test_strict_parser_rejects_invalid_native_data(data):
    with pytest.raises(ValueError):
        iter_sse_events('data: ' + data + '\n\n', strict=True)


def test_old_tolerant_parser_remains_compatible():
    assert iter_sse_events('data: bad\n\n') == []
    assert iter_sse_events('data: [DONE]\n\n') == ['[DONE]']


@pytest.mark.parametrize('separator', ['\n\n', '\r\n\r\n', '\r\r'])
def test_strict_parser_accepts_comments_and_valid_native_events(separator):
    raw = ': keepalive' + separator + 'event: ping\ndata: {"type":"ping"}' + separator
    assert iter_sse_events(raw, strict=True) == [{'type': 'ping'}]


def test_strict_parser_does_not_ignore_event_name_mismatch():
    with pytest.raises(ValueError):
        iter_sse_events('event: message_stop\ndata: {"type":"ping"}\n\n', strict=True)


@pytest.mark.parametrize('event', [
    {'type': 'message_start', 'message': {}},
    {'type': 'message_start', 'message': {'id': 'x', 'usage': {'output_tokens': 'bad'}}},
    {'type': 'content_block_start', 'index': True, 'content_block': {'type': 'text', 'text': ''}},
    {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'name': 'tool'}},
    {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': []}},
    {'type': 'content_block_delta', 'index': 0},
    {'type': 'error', 'error': 'secret-raw-error'},
])
def test_bad_shapes_return_safe_errors(event):
    with pytest.raises(ValueError, match='invalid upstream stream event') as failure:
        validate_stream_event(event)
    assert 'secret' not in str(failure.value)


def test_strict_accumulator_preserves_tools_and_signatures():
    accumulator = MessageAccumulator(strict=True)
    for event in sample_events(tools=True):
        accumulator.feed(event)
    message = accumulator.finish()
    assert message['id'] == 'msg_test'
    assert message['content'][0]['signature'] == 'opaque_signature'
    assert all(block['input'] == {'path': 'test.txt'} for block in message['content'] if block['type'] == 'tool_use')


def test_unknown_tool_delta_is_not_a_keyerror():
    translator = OpenAITranslator('GLM-5.3')
    translator.feed(sample_events()[0])
    with pytest.raises(ValueError):
        translator.feed({'type': 'content_block_delta', 'index': 42,
                         'delta': {'type': 'input_json_delta', 'partial_json': '{}'}})


def test_strict_completion_rejects_open_blocks_and_bad_tool_arguments():
    accumulator = MessageAccumulator(strict=True)
    accumulator.feed(sample_events()[0]); accumulator.feed(sample_events()[1])
    with pytest.raises(ValueError):
        accumulator.feed({'type': 'message_stop'})
    accumulator = MessageAccumulator(strict=True)
    events = sample_events(tools=True)
    for event in events:
        if event.get('index') == 2 and event.get('delta', {}).get('type') == 'input_json_delta':
            event = {**event, 'delta': {'type': 'input_json_delta', 'partial_json': '['}}
        accumulator.feed(event)
    with pytest.raises(ValueError, match='invalid upstream tool arguments'):
        accumulator.finish()
