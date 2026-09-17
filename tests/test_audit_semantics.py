"""Migration representation regressions, with no real conversation fixtures."""
import copy
import json
import shutil
import unittest
from unittest import mock

import test_audit as base
import test_sync as fixtures
from agent_sync import audit
from agent_sync.audit_semantics import completed_event_coverage
from agent_sync.files import digest, encode


class MigrationAuditTests(unittest.TestCase):
    def setUp(self):
        self.case = base.AuditTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def compare(self, left, right):
        with mock.patch.object(audit, 'native_turns', return_value=[]):
            return audit.compare_codex(self.case.rel, fixtures.lines(*left), fixtures.lines(*right), {}, {})

    def test_null_alias_and_text_wrappers_are_equal(self):
        for local, alternative in [({'instructions': None}, {'base_instructions': None}),
                ({'instructions': 'Follow these instructions'}, {'base_instructions': {'text': 'Follow these instructions'}}),
                ({}, {'base_instructions': {'text': None}, 'developer_instructions': None})]:
            a, b = copy.deepcopy(self.case.rows), copy.deepcopy(self.case.rows)
            a[0]['payload'].update(local); b[0]['payload'].update(alternative)
            result = self.compare(a, b)
            self.assertEqual(result['classification'], 'equivalent_content')
            self.assertEqual(result['sections']['instruction_context']['relation'], 'equal')

    def test_empty_session_retains_32374_local_instruction_characters(self):
        a, b = [copy.deepcopy(self.case.rows[0])], [copy.deepcopy(self.case.rows[0])]
        a[0]['payload']['instructions'] = 'x' * 32374
        b[0]['payload']['base_instructions'] = None
        result = self.compare(a, b)
        self.assertEqual(result['classification'], 'local_additional_content')
        self.assertEqual(result['sections']['instruction_context']['local_session_instruction_characters'], 32374)

    def test_changed_instruction_and_extra_wrapper_fields_are_not_equivalent(self):
        for value in ['Changed instructions', {'text': 'Original', 'new_constraint': 'Preserve this'}]:
            a, b = copy.deepcopy(self.case.rows), copy.deepcopy(self.case.rows)
            a[0]['payload']['instructions'] = 'Original'
            b[0]['payload']['base_instructions'] = value
            self.assertEqual(self.compare(a, b)['classification'], 'divergent_content')

    def test_initial_instruction_carrier_requires_exact_authoritative_content(self):
        for role, text, covered in [('developer', 'Original instructions', True),
                ('user', '<user_instructions>\nOriginal instructions\n</user_instructions>', True),
                ('user', 'Please quote Original instructions', False),
                ('developer', 'Original instructions plus a changed constraint', False)]:
            a, b = copy.deepcopy(self.case.rows), copy.deepcopy(self.case.rows)
            carrier = {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
                       'content': [{'type': 'input_text', 'text': text}]}}
            a.insert(1, carrier); b.insert(1, carrier)
            a[0]['payload']['instructions'] = 'Original instructions'
            result = self.compare(a, b)
            self.assertEqual(result['classification'], 'equivalent_content' if covered else 'local_additional_content')
            self.assertEqual(result['sections']['instruction_context']['covered_elsewhere']['local_count'], int(covered))
        # An exact phrase later in the dialogue cannot establish initial coverage.
        a, b = copy.deepcopy(self.case.rows), copy.deepcopy(self.case.rows)
        a[0]['payload']['instructions'] = 'Original instructions'
        later = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'developer',
                 'content': [{'type': 'input_text', 'text': 'Original instructions'}]}}
        a.append(later); b.append(later)
        self.assertEqual(self.compare(a, b)['classification'], 'local_additional_content')

    def test_turn_context_normalizes_instruction_representation_not_policy(self):
        a, b = copy.deepcopy(self.case.rows), copy.deepcopy(self.case.rows)
        ctx = {'type': 'turn_context', 'payload': {'turn_id': 't1', 'approval_policy': 'never',
               'developer_instructions': {'text': 'Constraint'}}}
        a.append(copy.deepcopy(ctx)); b.append(copy.deepcopy(ctx))
        b[-1]['payload']['developer_instructions'] = 'Constraint'
        b[-1]['payload']['user_instructions'] = None
        self.assertEqual(self.compare(a, b)['classification'], 'equivalent_content')
        b[-1]['payload']['approval_policy'] = 'always'
        self.assertEqual(self.compare(a, b)['classification'], 'divergent_content')

    def completed(self, item, turn_id='turn-1', **extras):
        return {'type': 'event_msg', 'payload': dict(type='item_completed', thread_id=base.TID,
                turn_id=turn_id, item=item, completed_at_ms=123, **extras)}

    def bind(self, rows, obj):
        data = fixtures.lines(*rows)
        obj = copy.deepcopy(obj)
        obj['rollout_sha256'] = digest(data)
        obj['tables']['thread_history_projection_state'][0]['next_rollout_byte_offset'] = len(data)
        obj['tables']['thread_history_projection_state'][0]['next_rollout_ordinal'] = len(rows)
        path = '.agent-sync-history/{}/{}.json'.format(base.TID, digest(data))
        return data, {path: [encode(obj)]}

    def test_completion_coverage_checks_identity_content_unknown_fields_and_order(self):
        obj = json.loads(self.case.exports[self.case.export_rel][0])
        native_item = json.loads(obj['tables']['thread_items'][0]['item_json'])
        event_item = {'type': 'UserMessage', 'id': native_item['id'], 'content': native_item['content']}
        event = self.completed(event_item)
        self.assertEqual(len(completed_event_coverage([event], base.TID, obj)[0]), 1)
        for mutation in ('thread', 'turn', 'id', 'text', 'extra', 'future_type'):
            e = copy.deepcopy(event)
            if mutation == 'thread': e['payload']['thread_id'] = 'different-thread'
            elif mutation == 'turn': e['payload']['turn_id'] = 'different-turn'
            elif mutation == 'id': e['payload']['item']['id'] = 'different-item'
            elif mutation == 'text': e['payload']['item']['content'][0]['text'] = 'not the same content'
            elif mutation == 'extra': e['payload']['item']['new_field'] = 'not covered'
            else: e['payload']['item']['type'] = 'FutureItem'
            covered, unknown = completed_event_coverage([e], base.TID, obj)
            self.assertEqual(len(covered), 0, mutation)
            self.assertEqual(len(unknown), 1)

    def test_reasoning_fragments_cover_ordered_accumulation_and_duplicates(self):
        obj = json.loads(self.case.exports[self.case.export_rel][0])
        item = obj['tables']['thread_items'][0]
        item.update(item_type='reasoning', item_json=json.dumps({'id': 'item-1', 'type': 'reasoning',
                    'summary': ['same', 'middle', 'same'], 'content': []}))
        events = [self.completed({'type': 'Reasoning', 'id': 'item-1', 'summary_text': list(s), 'raw_content': []})
                  for s in (('same',), ('same', 'middle'), ('same', 'middle', 'same'))]
        self.assertEqual(len(completed_event_coverage(events, base.TID, obj)[0]), 3)
        covered, unknown = completed_event_coverage([events[0], events[2], events[1]], base.TID, obj)
        self.assertTrue(unknown)
        self.assertLess(len(covered), 3)
        extra = events + [events[0]]
        self.assertEqual(len(completed_event_coverage(extra, base.TID, obj)[1]), 1)

    def test_rollback_finding_survives_unrelated_unknown_event(self):
        a, b = copy.deepcopy(self.case.rows), copy.deepcopy(self.case.rows)
        for typ in ('message', 'reasoning', 'message'):
            a.append({'type': 'response_item', 'payload': {'type': typ, 'retained': 'archival content'}})
        a.append({'type': 'event_msg', 'payload': {'type': 'thread_rolled_back', 'num_turns': 1}})
        b.append({'type': 'event_msg', 'payload': {'type': 'future_migration_event', 'content': 'unknown'}})
        result = self.compare(a, b)
        self.assertEqual(result['classification'], 'inconclusive')
        self.assertEqual(result['known_content_classification'], 'local_additional_content')
        self.assertEqual(result['findings']['response_records'], 'local_additional')
        self.assertEqual(result['findings']['rollback_history'], 'local_additional')
        self.assertEqual(result['sections']['unrecognized_records']['alternative_types'],
                         {'event_msg/future_migration_event': 1})
        b.pop()
        self.assertEqual(self.compare(a, b)['classification'], 'local_additional_content')

    def test_120_vs_117_archival_records_with_covered_migration_event(self):
        a = copy.deepcopy(self.case.rows)
        a += [{'type': 'response_item', 'payload': {'type': 'reasoning', 'summary': [str(i)]}}
              for i in range(116)]
        b = copy.deepcopy(a)
        a += [{'type': 'response_item', 'payload': {'type': kind, 'archival': role}}
              for kind, role in [('message', 'user'), ('reasoning', 'reasoning'), ('message', 'assistant')]]
        a.append({'type': 'event_msg', 'payload': {'type': 'thread_rolled_back', 'num_turns': 1}})
        b[0]['payload']['history_mode'] = 'paginated'
        obj = json.loads(self.case.exports[self.case.export_rel][0])
        item = json.loads(obj['tables']['thread_items'][0]['item_json'])
        b.append(self.completed({'type': 'UserMessage', 'id': item['id'], 'content': item['content']}))
        data, exports = self.bind(b, obj)
        with mock.patch.object(audit, 'native_turns', return_value=[]):
            result = audit.compare_codex(self.case.rel, fixtures.lines(*a), data, {}, exports)
        self.assertEqual(result['classification'], 'local_additional_content', result)
        self.assertEqual(result['sections']['response_records']['local_count'], 120)
        self.assertEqual(result['sections']['response_records']['alternative_count'], 117)

    def test_command_representation_preserves_args_paths_results_and_status(self):
        obj = json.loads(self.case.exports[self.case.export_rel][0])
        record = obj['tables']['thread_items'][0]
        item = {'type': 'CommandExecution', 'id': 'item-1', 'command': ['cat', 'a b.txt'],
                'cwd': 'file:///tmp/project%20one', 'process_id': None, 'source': 'agent',
                'status': 'completed', 'aggregated_output': 'Original output', 'exit_code': 0,
                'duration': {'secs': 1, 'nanos': 5000000},
                'parsed_cmd': [{'type': 'read', 'cmd': 'cat "a b.txt"', 'name': 'a b.txt', 'path': 'a b.txt'}]}
        native = {'type': 'commandExecution', 'id': 'item-1', 'command': 'cat "a b.txt"',
                  'cwd': '/tmp/project one', 'processId': None, 'source': 'agent', 'status': 'completed',
                  'aggregatedOutput': 'Original output', 'exitCode': 0, 'durationMs': 1005,
                  'commandActions': [{'type': 'read', 'command': 'cat "a b.txt"', 'name': 'a b.txt',
                                      'path': '/tmp/project one/a b.txt'}]}
        record.update(item_type='commandExecution', item_json=json.dumps(native))
        self.assertEqual(len(completed_event_coverage([self.completed(item)], base.TID, obj)[0]), 1)
        for field, value in [('command', ['cat', 'different.txt']), ('aggregated_output', 'Changed output'),
                             ('exit_code', 1), ('status', 'failed'), ('cwd', 'file:///different/project')]:
            changed = dict(item, **{field: value})
            self.assertEqual(len(completed_event_coverage([self.completed(changed)], base.TID, obj)[1]), 1, field)

    def test_mcp_and_websearch_conversions_keep_results_errors_and_queries(self):
        obj = json.loads(self.case.exports[self.case.export_rel][0])
        record = obj['tables']['thread_items'][0]
        mcp = {'type': 'McpToolCall', 'id': 'item-1', 'server': 'synthetic', 'tool': 'lookup',
               'arguments': {'key': 'a'}, 'status': 'completed',
               'result': {'content': [{'type': 'text', 'text': 'Original'}], 'isError': False},
               'duration': {'secs': 0, 'nanos': 999999}}
        native = {'type': 'mcpToolCall', 'id': 'item-1', 'server': 'synthetic', 'tool': 'lookup',
                  'arguments': {'key': 'a'}, 'status': 'completed', 'error': None,
                  'result': {'content': [{'type': 'text', 'text': 'Original'}], 'structuredContent': None}, 'durationMs': 0}
        record.update(item_type='mcpToolCall', item_json=json.dumps(native))
        self.assertEqual(len(completed_event_coverage([self.completed(mcp)], base.TID, obj)[0]), 1)
        changed = copy.deepcopy(mcp); changed['result']['isError'] = True
        self.assertTrue(completed_event_coverage([self.completed(changed)], base.TID, obj)[1])
        changed = copy.deepcopy(mcp); changed['result']['content'][0]['text'] = 'Changed'
        self.assertTrue(completed_event_coverage([self.completed(changed)], base.TID, obj)[1])
        web = {'type': 'WebSearch', 'id': 'item-1', 'query': 'test', 'action': {'type': 'open_page', 'url': 'https://example.com'}}
        native = {'type': 'webSearch', 'id': 'item-1', 'query': 'test', 'action': {'type': 'openPage', 'url': 'https://example.com'}}
        record.update(item_type='webSearch', item_json=json.dumps(native))
        self.assertEqual(len(completed_event_coverage([self.completed(web)], base.TID, obj)[0]), 1)

    @unittest.skipUnless(shutil.which('codex'), 'Codex CLI is not installed')
    def test_native_covered_completion_is_equivalent_and_sources_unchanged(self):
        obj = json.loads(self.case.exports[self.case.export_rel][0])
        native_item = json.loads(obj['tables']['thread_items'][0]['item_json'])
        events = [self.completed({'type': 'UserMessage', 'id': native_item['id'], 'content': native_item['content']})]
        rows = [json.loads(line) for line in self.case.paginated.splitlines()] + events
        data, exports = self.bind(rows, obj)
        before = self.case.tree()
        result = audit.compare_codex(self.case.rel, self.case.legacy, data, {}, exports)
        self.assertEqual(result['classification'], 'equivalent_content', result)
        self.assertEqual(result['migration_events']['alternative']['covered_count'], 1)
        self.assertEqual(result['sections']['unrecognized_records']['relation'], 'equal')
        self.assertEqual(self.case.tree(), before)
