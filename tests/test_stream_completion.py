"""Completion and tool arguments must match the downstream wire representation."""
import json

import pytest

from test_stream_retry import cfg, service, REQ
from tests_fixtures import FakeClient, FakeResponse, frame, sample_events
from zcode_gateway.service import UpstreamFailure
from zcode_gateway.zcode_protocol import MessageAccumulator, OpenAITranslator


def tool_events(initial=None, partial=None):
    events = [sample_events()[0],
              {'type': 'content_block_start', 'index': 0, 'content_block': {
                  'type': 'tool_use', 'id': 'tool_empty', 'name': 'inspect',
                  'input': {} if initial is None else initial}}]
    if partial is not None:
        events.append({'type': 'content_block_delta', 'index': 0, 'delta': {
            'type': 'input_json_delta', 'partial_json': partial}})
    events.extend([{'type': 'content_block_stop', 'index': 0},
                   {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'},
                    'usage': {'output_tokens': 1}},
                   {'type': 'message_stop'}])
    return events


def translated_arguments(events):
    translator = OpenAITranslator('GLM-5.3')
    chunks = [chunk for event in events for chunk in translator.feed(event)]
    return ''.join(call['function'].get('arguments', '')
                   for chunk in chunks for choice in chunk['choices']
                   for call in choice['delta'].get('tool_calls', []))


@pytest.mark.parametrize('reason', [None, 'garbage'])
def test_strict_completion_requires_valid_terminal_delta(reason):
    events = sample_events()
    if reason is None:
        events = [event for event in events if event['type'] != 'message_delta']
    else:
        events[-2]['delta']['stop_reason'] = reason
    accumulator = MessageAccumulator(strict=True)
    with pytest.raises(ValueError):
        for event in events:
            accumulator.feed(event)
        accumulator.finish()


@pytest.mark.parametrize('reason,finish', [('end_turn', 'stop'), ('stop_sequence', 'stop'),
                                         ('max_tokens', 'length'), ('tool_use', 'tool_calls'),
                                         ('pause_turn', 'stop'), ('refusal', 'content_filter')])
def test_recognized_terminal_reasons_are_preserved(reason, finish):
    events = sample_events(tools=reason == 'tool_use')
    events[-2]['delta']['stop_reason'] = reason
    accumulator, translator = MessageAccumulator(strict=True), OpenAITranslator('GLM-5.3')
    chunks = []
    for event in events:
        accumulator.feed(event)
        chunks.extend(translator.feed(event))
    assert accumulator.finish()['stop_reason'] == reason
    assert [choice['finish_reason'] for chunk in chunks for choice in chunk['choices']
            if choice['finish_reason'] is not None] == [finish]


@pytest.mark.parametrize('kind', ['duplicate_terminal', 'content_after_terminal', 'early_terminal'])
def test_strict_completion_enforces_terminal_order(kind):
    events = sample_events()
    if kind == 'duplicate_terminal':
        events.insert(-1, events[-2])
    elif kind == 'content_after_terminal':
        events.insert(-1, {'type': 'content_block_start', 'index': 2,
                          'content_block': {'type': 'text', 'text': ''}})
        events.insert(-1, {'type': 'content_block_stop', 'index': 2})
    else:
        events.insert(2, events.pop(-2))
    accumulator = MessageAccumulator(strict=True)
    with pytest.raises(ValueError):
        for event in events:
            accumulator.feed(event)
        accumulator.finish()


@pytest.mark.parametrize('initial,partial', [({}, None), ({'path': 'test.txt'}, None),
                                           ({}, '{"path":"test.txt"}')])
def test_completed_tool_arguments_match_accumulator(initial, partial):
    events = tool_events(initial, partial)
    accumulator = MessageAccumulator(strict=True)
    for event in events:
        accumulator.feed(event)
    assert json.loads(translated_arguments(events)) == accumulator.finish()['content'][0]['input']


@pytest.mark.parametrize('consumer', [MessageAccumulator, OpenAITranslator])
def test_initial_tool_input_plus_json_deltas_is_rejected(consumer):
    receiver = consumer(strict=True) if consumer is MessageAccumulator else consumer('GLM-5.3')
    with pytest.raises(ValueError, match='tool arguments'):
        for event in tool_events({'path': 'initial.txt'}, '{"path":"delta.txt"}'):
            receiver.feed(event)
        if consumer is MessageAccumulator:
            receiver.finish()


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('bad_case', ['missing_stop_reason', 'unknown_stop_reason', 'mixed_tool_input'])
async def test_verified_rejects_invalid_completion_before_delivery(service, native, bad_case):
    events = sample_events()
    if bad_case == 'missing_stop_reason':
        events.pop(-2)
    elif bad_case == 'unknown_stop_reason':
        events[-2]['delta']['stop_reason'] = 'garbage'
    else:
        events = tool_events({'path': 'initial.txt'}, '{"path":"delta.txt"}')
    service.client = FakeClient(responses=[FakeResponse(b''.join(frame(e) for e in events))])
    with pytest.raises(UpstreamFailure) as error:
        await service.verified_stream(dict(REQ), model='GLM-5.3', anthropic=native)
    assert error.value.code == 'invalid_stream'
    assert len(service.client.calls) == 1
    assert service.client.responses[0].closed
    assert (await service.pool.all())[0].in_flight == 0


async def test_verified_no_argument_tool_delivers_json_object(service):
    service.client = FakeClient(responses=[FakeResponse(b''.join(frame(e) for e in tool_events()))])
    result = await service.verified_stream(dict(REQ), model='GLM-5.3')
    chunks = [json.loads(line[6:]) for line in b''.join(result.frames).decode().splitlines()
              if line.startswith('data: {')]
    arguments = ''.join(call['function'].get('arguments', '')
                       for chunk in chunks for choice in chunk['choices']
                       for call in choice['delta'].get('tool_calls', []))
    assert json.loads(arguments) == {}
    assert any(choice['finish_reason'] == 'tool_calls' for chunk in chunks for choice in chunk['choices'])
    assert b''.join(result.frames).count(b'data: [DONE]') == 1
