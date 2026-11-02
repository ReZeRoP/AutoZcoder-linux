"""ZCode's captured Anthropic wire contract and OpenAI adapter."""
from __future__ import annotations

import copy
import json
import time
import uuid
from .zcode_bootstrap import bootstrap_system


def reasoning(payload: dict, default_effort: str = 'high') -> dict:
    thinking = payload.get('thinking')
    effort = (payload.get('output_config') or {}).get('effort')
    effort = effort or payload.get('reasoning_effort') or payload.get('reasoningEffort')
    effort = effort or (payload.get('reasoning') or {}).get('effort')
    disabled = (payload.get('enable_thinking') is False
                or (payload.get('reasoning') or {}).get('enabled') is False
                or (isinstance(thinking, dict) and thinking.get('type') == 'disabled')
                or effort in ('none', 'off', 'disabled'))
    if disabled:
        return {'thinking': {'type': 'disabled'}}
    mapped = {'minimal': 'low', 'low': 'low', 'medium': 'high', 'high': 'high',
              'xhigh': 'max', 'extra': 'max', 'max': 'max'}.get(effort or default_effort)
    if mapped is None:
        raise ValueError('reasoning effort must be low, medium, high, max, or none')
    return {'thinking': {'type': 'enabled'}, 'output_config': {'effort': mapped}}


def content_blocks(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content else []
    if not isinstance(content, list):
        raise ValueError('message content must be a string or a list of blocks')
    out = []
    for part in content:
        if not isinstance(part, dict):
            raise ValueError('content blocks must be objects')
        kind = part.get('type')
        if kind in ('text', 'input_text'):
            out.append({'type': 'text', 'text': part.get('text', '')})
        elif kind == 'image_url':
            image = part.get('image_url')
            url = image.get('url') if isinstance(image, dict) else image
            if not isinstance(url, str):
                raise ValueError('image_url.url must be a string')
            if url.startswith('data:'):
                header, sep, data = url.partition(',')
                if not sep or ';base64' not in header:
                    raise ValueError('images must use a base64 data URL')
                source = {'type': 'base64', 'media_type': header[5:].split(';')[0], 'data': data}
            else:
                source = {'type': 'url', 'url': url}
            out.append({'type': 'image', 'source': source})
        elif kind in ('image', 'tool_use', 'tool_result', 'thinking', 'redacted_thinking'):
            out.append(copy.deepcopy(part))
        else:
            raise ValueError(f'unsupported content block type: {kind}')
    return out


def _strip_image_blocks(messages) -> None:
    """Drop image blocks (and empty image-only tool_result contents) from an
    Anthropic-shape message list, in place. Used for non-vision models: the
    upstream would 400 the whole request; the text proceeds instead."""
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get('content')
        if not isinstance(content, list):
            continue
        message['content'] = [b for b in content
                              if not (isinstance(b, dict) and b.get('type') == 'image')]


def _strip_image_blocks_system(system) -> None:
    pass  # system is always text-only; images there already 400 in content_blocks


def build_body(payload: dict, default_max_tokens=128000, model_caps=None,
               default_effort='high', harness=True) -> dict:
    """Native requests preserve blocks, caching, signatures and metadata exactly.

    `model_caps` (optional): the registry's per-model capability dict — when
    present, image blocks are stripped for non-vision models (the request
    proceeds text-only instead of a guaranteed upstream 400) and max_tokens is
    clamped to the model's real output cap.
    `harness` (default True): inject the full captured ZCode harness block;
    False injects the identity line only (upstream.bootstrap_harness: false)."""
    vision = None
    if isinstance(model_caps, dict):
        vision = bool(model_caps.get('vision'))
    if payload.get('_anthropic'):
        body = copy.deepcopy(payload['_anthropic'])
        body.pop('account', None)
        body.pop('account_id', None)
        body['model'] = payload['model']
        # capability gate applies to the native path too: an Anthropic client
        # can send image blocks to a text-only model just like an OpenAI one
        if vision is False:
            _strip_image_blocks(body.get('messages') or [])
            _strip_image_blocks_system(body.get('system'))
    else:
        body = {'model': payload['model'], 'messages': []}
        system = []
        for message in payload.get('messages', []):
            role = message.get('role')
            blocks = content_blocks(message.get('content'))
            if role in ('system', 'developer'):
                if any(block.get('type') != 'text' for block in blocks):
                    raise ValueError('system messages must contain text')
                system.extend(blocks)
                continue
            if role == 'tool':
                blocks = [{'type': 'tool_result', 'tool_use_id': message.get('tool_call_id'),
                           'content': blocks}]
                role = 'user'
            elif role == 'assistant':
                for call in message.get('tool_calls') or []:
                    function = call.get('function') or {}
                    try:
                        args = function.get('arguments') or '{}'
                        args = json.loads(args) if isinstance(args, str) else args
                    except (ValueError, TypeError) as exc:
                        raise ValueError('tool call arguments must be valid JSON') from exc
                    blocks.append({'type': 'tool_use', 'id': call.get('id'),
                                   'name': function.get('name'), 'input': args})
            if role not in ('user', 'assistant'):
                raise ValueError(f'unsupported message role: {role}')
            if vision is False:
                blocks = [b for b in blocks if b.get('type') != 'image']
            # Anthropic expects tool results in user messages and alternating turns.
            if body['messages'] and body['messages'][-1]['role'] == role:
                body['messages'][-1]['content'].extend(blocks)
            else:
                body['messages'].append({'role': role, 'content': blocks})
        if system:
            body['system'] = system
        if payload.get('tools'):
            body['tools'] = []
            for tool in payload['tools']:
                if tool.get('type') != 'function':
                    raise ValueError('only function tools are supported')
                fn = tool['function']
                body['tools'].append({'name': fn['name'], 'description': fn.get('description', ''),
                                      'input_schema': fn.get('parameters') or {'type': 'object'}})
            choice = payload.get('tool_choice')
            if choice is None:
                # explicit JSON null (or absent) — let the upstream default.
                # The old code skipped setting the key entirely and the
                # parallel_tool_calls branch below then crashed on KeyError.
                body['tool_choice'] = {'type': 'auto'} if payload.get('parallel_tool_calls') is False else None
                if body['tool_choice'] is None:
                    body.pop('tool_choice', None)
            elif isinstance(choice, str):
                if choice == 'none':
                    # Handled after conversion, including history without definitions.
                    pass
                else:
                    body['tool_choice'] = {'type': {'required': 'any'}.get(choice, choice)}
            elif isinstance(choice, dict):
                body['tool_choice'] = {'type': 'tool', 'name': choice['function']['name']}
            if payload.get('parallel_tool_calls') is False and body.get('tool_choice') is not None:
                body['tool_choice']['disable_parallel_tool_use'] = True
        for key in ('temperature', 'top_p', 'top_k', 'metadata'):
            if payload.get(key) is not None:
                body[key] = copy.deepcopy(payload[key])
        if payload.get('stop') is not None:
            stop = payload['stop']
            body['stop_sequences'] = [stop] if isinstance(stop, str) else stop
        if payload.get('response_format') is not None:
            raise ValueError('response_format is not supported by the captured ZCode API')
        body.update(reasoning(payload, default_effort))
    choice = body.get('tool_choice') if payload.get('_anthropic') else payload.get('tool_choice')
    if choice == 'none' or (isinstance(choice, dict) and choice.get('type') == 'none'):
        if any(b.get('type') in ('tool_use', 'tool_result')
               for m in body.get('messages', []) if isinstance(m.get('content'), list)
               for b in m['content'] if isinstance(b, dict)):
            raise ValueError('tool_choice none with tool history is not supported by the captured ZCode API')
        body.pop('tools', None)
        body.pop('tool_choice', None)
    limit = body.get('max_tokens')
    if limit is None:
        limit = payload.get('max_tokens')
    if limit is None:
        limit = payload.get('max_completion_tokens')
    if limit is None:
        limit = default_max_tokens
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError('max_tokens must be a positive integer')
    # clamp to the model's real output cap so a client asking for 500k on a
    # 128k model gets 128k instead of an upstream rejection
    if isinstance(model_caps, dict):
        cap = int(model_caps.get('max_output_tokens', 0) or 0)
        if cap > 0:
            limit = min(limit, cap)
    body['max_tokens'] = limit
    body['stream'] = True
    if 'thinking' not in body:
        body.update(reasoning(body, default_effort))
    if (body.get('thinking') or {}).get('type') == 'disabled':
        raise ValueError('ZCode GLM models always use reasoning. Use reasoning_effort low, high, or max; disabling reasoning is not supported.')
    body['system'] = bootstrap_system(body.get('system'), harness=harness)
    return body


def usage_openai(usage: dict) -> dict:
    cache = int(usage.get('cache_read_input_tokens') or 0)
    prompt = int(usage.get('input_tokens') or 0) + cache + int(usage.get('cache_creation_input_tokens') or 0)
    output = int(usage.get('output_tokens') or 0)
    return {'prompt_tokens': prompt, 'completion_tokens': output, 'total_tokens': prompt + output,
            'prompt_tokens_details': {'cached_tokens': cache}}


STOP_REASONS = {'end_turn': 'stop', 'stop_sequence': 'stop', 'max_tokens': 'length',
                'tool_use': 'tool_calls', 'pause_turn': 'stop', 'refusal': 'content_filter'}


def validate_stream_event(event):
    """Validate known wire fields without including upstream values in errors."""
    def require(ok):
        if not ok:
            raise ValueError('invalid upstream stream event')
    require(isinstance(event, dict))
    kind = event.get('type')
    if kind is None and isinstance(event.get('error'), dict):
        kind = 'error'
    require(kind in ('message_start', 'content_block_start', 'content_block_delta',
                     'content_block_stop', 'message_delta', 'message_stop', 'ping', 'error'))
    if kind == 'error':
        require(isinstance(event.get('error'), dict))
        return
    if kind == 'message_start':
        message = event.get('message')
        require(isinstance(message, dict))
        require(isinstance(message.get('id'), str) and bool(message['id']))
        require(isinstance(message.get('content', []), list))
        usage = message.get('usage', {})
    else:
        usage = event.get('usage', {})
    require(isinstance(usage, dict))
    for key in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'):
        if key in usage:
            require(type(usage[key]) is int and 0 <= usage[key] <= 2 ** 63 - 1)
    if kind.startswith('content_block_'):
        require(type(event.get('index')) is int and 0 <= event['index'] <= 4096)
    if kind == 'content_block_start':
        block = event.get('content_block')
        require(isinstance(block, dict))
        require(block.get('type') in ('text', 'thinking', 'redacted_thinking', 'tool_use'))
        for key in ('text', 'thinking', 'signature', 'data'):
            if key in block:
                require(isinstance(block[key], str))
        if block['type'] == 'tool_use':
            require(isinstance(block.get('id'), str) and bool(block['id']))
            require(isinstance(block.get('name'), str) and bool(block['name']))
            require(isinstance(block.get('input', {}), dict))
    if kind == 'content_block_delta':
        delta = event.get('delta')
        require(isinstance(delta, dict))
        key = {'text_delta': 'text', 'thinking_delta': 'thinking',
               'signature_delta': 'signature', 'input_json_delta': 'partial_json'}.get(delta.get('type'))
        require(key is not None and isinstance(delta.get(key), str))
    if kind == 'message_delta':
        delta = event.get('delta')
        require(isinstance(delta, dict))
        stop = delta.get('stop_reason')
        require(stop is None or (isinstance(stop, str) and stop in STOP_REASONS))
        require(delta.get('stop_sequence') is None or isinstance(delta['stop_sequence'], str))


class MessageAccumulator:
    def __init__(self, strict=False):
        self.message = {'id': '', 'type': 'message', 'role': 'assistant', 'content': [],
                        'stop_reason': None, 'stop_sequence': None, 'usage': {}}
        self.blocks = {}
        self.partial_json = {}
        self.stopped = False
        self.strict = strict
        self.started = False
        self.closed_blocks = set()
        self.terminal = False

    def feed(self, event: dict):
        validate_stream_event(event)
        kind = event.get('type')
        if self.strict:
            if self.stopped or (not self.started and kind not in ('message_start', 'ping', 'error')):
                raise ValueError('invalid upstream stream ordering')
            if kind == 'message_start':
                if self.started:
                    raise ValueError('duplicate upstream message start')
                self.started = True
            if self.terminal and kind not in ('message_stop', 'ping', 'error'):
                raise ValueError('upstream content after terminal delta')
            if kind == 'message_delta' and event['delta'].get('stop_reason') is not None:
                if self.closed_blocks != set(self.blocks):
                    raise ValueError('upstream terminal delta with open content blocks')
                self.terminal = True
            if kind == 'content_block_start' and event['index'] in self.blocks:
                raise ValueError('duplicate upstream content block')
            if kind in ('content_block_delta', 'content_block_stop'):
                if event['index'] not in self.blocks or event['index'] in self.closed_blocks:
                    raise ValueError('invalid upstream content block ordering')
            if kind == 'content_block_delta':
                block_kind = self.blocks[event['index']]['type']
                allowed = {'text': ('text_delta',), 'thinking': ('thinking_delta', 'signature_delta'),
                           'tool_use': ('input_json_delta',), 'redacted_thinking': ()}[block_kind]
                if event['delta']['type'] not in allowed:
                    raise ValueError('upstream delta does not match its content block')
                if block_kind == 'tool_use' and self.blocks[event['index']].get('input'):
                    raise ValueError('upstream tool arguments mix initial input and deltas')
            if kind == 'content_block_stop':
                self.closed_blocks.add(event['index'])
            if kind == 'message_stop':
                if self.closed_blocks != set(self.blocks):
                    raise ValueError('upstream stopped with open content blocks')
                if not self.terminal:
                    raise ValueError('upstream stopped without a terminal delta')
        if kind == 'message_start':
            self.message.update(copy.deepcopy(event['message']))
        elif kind == 'content_block_start':
            self.blocks[event['index']] = copy.deepcopy(event['content_block'])
        elif kind == 'content_block_delta':
            index, delta = event['index'], event['delta']
            block = self.blocks.get(index)
            if block is None:
                raise ValueError('delta without content block')
            dtype = delta.get('type')
            key = {'text_delta': 'text', 'thinking_delta': 'thinking', 'signature_delta': 'signature'}.get(dtype)
            if key:
                block[key] = block.get(key, '') + delta.get(key, '')
            elif dtype == 'input_json_delta':
                self.partial_json[index] = self.partial_json.get(index, '') + delta.get('partial_json', '')
        elif kind == 'message_delta':
            self.message.update(event.get('delta') or {})
            self.message['usage'].update(event.get('usage') or {})
        elif kind == 'message_stop':
            self.stopped = True

    def finish(self):
        if self.strict and not self.stopped:
            raise ValueError('upstream message is not complete')
        for index, partial in self.partial_json.items():
            try:
                value = json.loads(partial or '{}')
            except (ValueError, TypeError):
                raise ValueError('invalid upstream tool arguments') from None
            if not isinstance(value, dict):
                raise ValueError('upstream tool arguments must be an object')
            self.blocks[index]['input'] = value
        self.message['content'] = [self.blocks[index] for index in sorted(self.blocks)]
        return self.message


class OpenAITranslator:
    """Track Anthropic block indices separately from OpenAI tool-call indices."""
    def __init__(self, model: str):
        self.model, self.id = model, 'chatcmpl-' + uuid.uuid4().hex[:24]
        self.created = int(time.time())
        self.tools = {}
        self.tool_arguments = {}
        self.initial_tool_input = set()
        self.usage = {}

    def chunk(self, delta, finish=None):
        return {'id': self.id, 'object': 'chat.completion.chunk', 'created': self.created,
                'model': self.model, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}

    def feed(self, event):
        validate_stream_event(event)
        kind = event.get('type')
        if kind == 'message_start':
            msg = event['message']
            self.id = 'chatcmpl-' + msg['id']
            self.usage.update(msg.get('usage') or {})
            return [self.chunk({'role': 'assistant', 'content': ''})]
        if kind == 'content_block_start':
            block = event['content_block']
            if block.get('type') == 'tool_use':
                idx = len(self.tools)
                self.tools[event['index']] = idx
                initial = block.get('input')
                arguments = json.dumps(initial) if initial else ''
                self.tool_arguments[event['index']] = arguments
                if initial:
                    self.initial_tool_input.add(event['index'])
                return [self.chunk({'tool_calls': [{'index': idx, 'id': block['id'], 'type': 'function',
                         'function': {'name': block['name'], 'arguments': arguments}}]})]
            if block.get('text'):
                return [self.chunk({'content': block['text']})]
            if block.get('thinking'):
                return [self.chunk({'reasoning_content': block['thinking'], 'reasoning': block['thinking']})]
        if kind == 'content_block_delta':
            delta = event['delta']
            if delta['type'] == 'text_delta':
                return [self.chunk({'content': delta.get('text', '')})]
            if delta['type'] == 'thinking_delta':
                value = delta.get('thinking', '')
                return [self.chunk({'reasoning_content': value, 'reasoning': value})]
            if delta['type'] == 'input_json_delta':
                if event['index'] not in self.tools:
                    raise ValueError('upstream tool delta has no tool start')
                if event['index'] in self.initial_tool_input:
                    raise ValueError('upstream tool arguments mix initial input and deltas')
                arguments = delta.get('partial_json', '')
                self.tool_arguments[event['index']] += arguments
                return [self.chunk({'tool_calls': [{'index': self.tools[event['index']],
                         'function': {'arguments': arguments}}]})]
        if kind == 'content_block_stop' and event['index'] in self.tools:
            arguments = self.tool_arguments[event['index']]
            try:
                value = json.loads(arguments or '{}')
            except (ValueError, TypeError):
                raise ValueError('invalid upstream tool arguments') from None
            if not isinstance(value, dict):
                raise ValueError('upstream tool arguments must be an object')
            if not arguments:
                self.tool_arguments[event['index']] = '{}'
                return [self.chunk({'tool_calls': [{'index': self.tools[event['index']],
                         'function': {'arguments': '{}'}}]})]
        if kind == 'message_delta':
            self.usage.update(event.get('usage') or {})
            stop = (event.get('delta') or {}).get('stop_reason')
            finish = STOP_REASONS.get(stop)
            return [self.chunk({}, finish)] if stop else []
        if kind == 'message_stop':
            chunk = self.chunk({})
            chunk['choices'] = []
            chunk['usage'] = usage_openai(self.usage)
            return [chunk]
        return []
