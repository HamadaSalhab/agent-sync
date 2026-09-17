"""Conservative representation adapters for audit evidence, never sync policy."""

import json
import posixpath
import re
import shlex
from urllib.parse import unquote, urlsplit


INSTRUCTION_KEYS = {'instructions': 'base_instructions', 'base_instructions': 'base_instructions',
                    'developer_instructions': 'developer_instructions', 'user_instructions': 'user_instructions'}


def local_path(value):
    if isinstance(value, str) and value.startswith('file:'):
        uri = urlsplit(value)
        if uri.scheme == 'file' and uri.netloc in ('', 'localhost') and not uri.query and not uri.fragment and uri.path.startswith('/'):
            return unquote(uri.path)
    return value


def instruction_value(value):
    if isinstance(value, dict) and set(value) == {'text'}:
        return value['text']
    return value


def normalize_context(value):
    if isinstance(value, list):
        return [normalize_context(v) for v in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, child in value.items():
        if key in INSTRUCTION_KEYS:
            child = instruction_value(child)
            if child is None or child == '':
                continue
            name = INSTRUCTION_KEYS[key]
            # Conflicting aliases must remain visible, not overwrite each other.
            if name in result and result[name] != child:
                name = key + ':conflicting_alias'
            result[name] = child
        else:
            result[key] = normalize_context(child)
            if key == 'sandbox_policy' and isinstance(result[key], dict):
                policy = result[key]
                mode = policy.get('mode')
                if isinstance(mode, str) and ('type' not in policy or policy['type'] == mode):
                    policy['type'] = policy.pop('mode')
    return result


def instruction_envelope(text):
    """Remove exactly a known envelope, never strip instruction whitespace.

    Prefer the two-newline envelope when present. Ambiguous older envelopes
    can therefore remain unproven rather than erase an instruction's newlines.
    """
    for boundary in ('\n\n', '\n', ''):
        prefix = '<user_instructions>' + boundary
        suffix = boundary + '</user_instructions>'
        if text.startswith(prefix) and text.endswith(suffix) and len(text) >= len(prefix) + len(suffix):
            return text[len(prefix):-len(suffix)]
    prefix = re.match(r'\A# AGENTS\.md instructions for ([^\r\n]+)\n\n<INSTRUCTIONS>\n', text)
    suffix = '\n</INSTRUCTIONS>'
    if prefix and prefix.group(1).strip() and text.endswith(suffix) and len(text) >= prefix.end() + len(suffix):
        return text[prefix.end():-len(suffix)]
    return None


def initial_instruction_texts(rows):
    """Only authoritative initial messages / explicit instruction blocks qualify.

    Later dialogue, quoted text, and arbitrary substring matches never establish
    that an initial instruction was retained.
    """
    texts = set()
    for row in rows[1:]:
        p = row['payload']
        if row['type'] == 'event_msg' and p.get('type') == 'user_message':
            break
        if row['type'] != 'response_item':
            continue
        if p.get('type') != 'message':
            break
        role = p.get('role')
        chunks = p.get('content', [])
        if not isinstance(chunks, list):
            break
        content = []
        for chunk in chunks:
            if not isinstance(chunk, dict) or chunk.get('type') not in ('input_text', 'output_text') or not isinstance(chunk.get('text'), str):
                return texts
            content.append(chunk['text'])
        if role in ('system', 'developer'):
            texts.update(content)
            texts.add(''.join(content))
        elif role == 'user':
            # Require an instruction-only message, not a quotation in a request.
            instruction = instruction_envelope(''.join(content))
            if instruction is not None:
                texts.add(instruction)
            else:
                break
        else:
            break
    return texts


def instruction_records(rows, other_rows):
    carriers = initial_instruction_texts(other_rows)
    other_header = normalize_context({k: v for k, v in other_rows[0]['payload'].items() if 'instruction' in k})
    records, covered = [], []
    for row in rows:
        p = row['payload']
        if row['type'] == 'session_meta':
            fields = normalize_context({k: v for k, v in p.items() if 'instruction' in k})
            for key, value in sorted(fields.items()):
                record = {'scope': 'session', 'field': key, 'value': value}
                if other_header.get(key) != value and isinstance(value, str) and value in carriers:
                    covered.append(record)
                else:
                    records.append(record)
        elif row['type'] == 'turn_context':
            records.append({'type': 'turn_context', 'payload': normalize_context(p)})
    return records, covered


def duration_ms(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'secs', 'nanos'}:
        raise ValueError('unsupported duration')
    if any(type(value[k]) is not int or value[k] < 0 for k in value) or value['nanos'] >= 1000000000:
        raise ValueError('invalid duration')
    return value['secs'] * 1000 + value['nanos'] // 1000000


def user_content(content):
    if not isinstance(content, list):
        raise ValueError('unsupported user content')
    result = []
    for block in content:
        if not isinstance(block, dict):
            raise ValueError('unsupported user content block')
        if block.get('type') == 'image' and 'image_url' in block:
            if set(block) != {'type', 'image_url'} or not isinstance(block['image_url'], str):
                raise ValueError('unsupported image fields')
            block = {'type': 'image', 'url': block['image_url'], 'detail': None}
        result.append(block)
    return result


def convert_completed_item(item):
    """Known CoreTurnItem -> API fields, pinned to the tested 0.154 schema.

    Every supplied key is consumed or rejected. Optional API defaults may be
    absent in an event, but event content may never disappear from the check.
    """
    p = dict(item)
    kind = p.pop('type')
    out = {'type': kind[0].lower() + kind[1:], 'id': p.pop('id')}
    if kind == 'UserMessage':
        p['content'] = user_content(p['content'])
        mapping = {'content': 'content', 'client_id': 'clientId'}
    elif kind == 'AgentMessage':
        content = p.pop('content')
        if not isinstance(content, list) or any(set(c) != {'type', 'text'} or c['type'] != 'Text' or not isinstance(c['text'], str) for c in content):
            raise ValueError('unsupported agent message content')
        out['text'] = ''.join(c['text'] for c in content)
        mapping = {'phase': 'phase', 'memory_citation': 'memoryCitation', 'delivery': 'delivery', 'questions': 'questions'}
    elif kind == 'Reasoning':
        mapping = {'summary_text': 'summary', 'raw_content': 'content'}
    elif kind == 'WebSearch':
        if isinstance(p.get('action'), dict):
            action = dict(p['action'])
            action['type'] = {'open_page': 'openPage', 'find_in_page': 'findInPage'}.get(action.get('type'), action.get('type'))
            p['action'] = action
        mapping = {'query': 'query', 'action': 'action', 'results': 'results'}
    elif kind == 'McpToolCall':
        if 'duration' in p:
            out['durationMs'] = duration_ms(p.pop('duration'))
        if isinstance(p.get('result'), dict) and 'isError' in p['result']:
            result = dict(p['result'])
            if result.pop('isError') is not False or p.get('status') != 'completed' or p.get('error') is not None:
                raise ValueError('MCP error result needs explicit coverage')
            p['result'] = result
        mapping = {k: k for k in ('server', 'tool', 'arguments', 'status', 'result', 'error', 'mcpAppResourceUri')}
        mapping.update(plugin_id='pluginId', read_only_hint='readOnlyHint')
    elif kind == 'CommandExecution':
        command = p.pop('command')
        if not isinstance(command, list) or not all(isinstance(x, str) for x in command):
            raise ValueError('unsupported command arguments')
        out['command'] = shlex.join(command)
        if 'duration' in p:
            out['durationMs'] = duration_ms(p.pop('duration'))
        source = p.pop('source', 'agent')
        sources = {'agent': 'agent', 'user_shell': 'userShell', 'unified_exec_startup': 'unifiedExecStartup',
                   'unified_exec_interaction': 'unifiedExecInteraction'}
        out['source'] = sources[source]
        if 'cwd' in p:
            p['cwd'] = local_path(p['cwd'])
        actions = []
        for action in p.pop('parsed_cmd', []):
            fields = {'unknown': {'type', 'cmd'}, 'read': {'type', 'cmd', 'name', 'path'},
                      'search': {'type', 'cmd', 'query', 'path'}, 'list_files': {'type', 'cmd', 'path'}}
            if action.get('type') not in fields or set(action) != fields[action['type']]:
                raise ValueError('unsupported parsed command action')
            converted = {('command' if k == 'cmd' else k): v for k, v in action.items()}
            converted['type'] = {'list_files': 'listFiles'}.get(action['type'], action['type'])
            if 'path' in converted:
                converted['path'] = local_path(converted['path'])
                if action['type'] == 'read' and isinstance(converted['path'], str) and not converted['path'].startswith('/') and isinstance(p.get('cwd'), str):
                    converted['path'] = posixpath.join(p['cwd'], converted['path'])
            actions.append(converted)
        out['commandActions'] = actions
        mapping = {'cwd': 'cwd', 'process_id': 'processId', 'status': 'status', 'aggregated_output': 'aggregatedOutput',
                   'exit_code': 'exitCode', 'plugin_id': 'pluginId', 'script_path': 'scriptPath'}
        if p.get('aggregated_output') == '':
            p['aggregated_output'] = None
    else:
        raise ValueError('unsupported completed item type: ' + str(kind))
    if set(p) - set(mapping):
        raise ValueError('unsupported completed item fields')
    out.update({mapping[k]: value for k, value in p.items()})
    return out


def completed_event_coverage(rows, thread_id, export):
    """Partition migration events using exact identity and item-content evidence.

    Export coverage is checked independently by the native reader before this
    evidence can affect the comparison's classification.
    """
    stored, positions = {}, {}
    if export:
        turns = {row['turn_id']: row['rollout_ordinal'] for row in export['tables']['thread_turns']}
        for row in export['tables']['thread_items']:
            stored[row['turn_id'], row['item_id']] = json.loads(row['item_json'])
            positions[row['turn_id'], row['item_id']] = (turns.get(row['turn_id'], -1), row['rollout_ordinal'])
    covered, unknown = [], []
    previous = (-1, -1)
    reasoning_cursors = {}

    def includes(candidate, value):
        if isinstance(value, dict):
            return isinstance(candidate, dict) and all(includes(candidate.get(k), v) for k, v in value.items())
        if isinstance(value, list):
            return isinstance(candidate, list) and len(candidate) == len(value) and all(includes(a, b) for a, b in zip(candidate, value))
        return type(candidate) is type(value) and candidate == value

    for row in rows:
        p = row['payload']
        if row['type'] != 'event_msg' or p.get('type') != 'item_completed':
            continue
        try:
            if set(p) - {'type', 'thread_id', 'turn_id', 'item', 'completed_at_ms', 'started_at_ms'}:
                raise ValueError('unknown event fields')
            if p.get('thread_id') != thread_id:
                raise ValueError('wrong thread identity')
            item = convert_completed_item(p['item'])
            candidate = stored.get((p['turn_id'], item['id']))
            if candidate is None:
                raise ValueError('missing exact turn/item identity')
            next_cursors = dict(reasoning_cursors)
            for key, value in item.items():
                expected = candidate.get(key)
                if item['type'] == 'reasoning' and key in ('summary', 'content'):
                    # Lifecycle events contain cumulative item snapshots. Each
                    # must be an ordered prefix of the final stored item, and
                    # later snapshots cannot lose earlier occurrences.
                    cursor_key = (p['turn_id'], item['id'], key)
                    cursor = next_cursors.get(cursor_key, 0)
                    if not isinstance(value, list) or not isinstance(expected, list) or len(value) < cursor or expected[:len(value)] != value:
                        raise ValueError('reasoning fragments are not covered in order')
                    next_cursors[cursor_key] = len(value)
                elif key == 'command' and item['type'] == 'commandExecution':
                    if not isinstance(expected, str) or shlex.split(expected) != shlex.split(value):
                        raise ValueError('command arguments are not covered')
                elif not includes(expected, value):
                    raise ValueError('completed item field is not covered: ' + key)
            position = positions[p['turn_id'], item['id']]
            if position < previous:
                raise ValueError('completed item order differs from exported history')
            previous = position
            reasoning_cursors = next_cursors
            covered.append(row)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            unknown.append((row, str(exc)))
    return covered, unknown
