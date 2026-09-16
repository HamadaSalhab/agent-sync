"""Synthetic audit cases. Native checks use only temporary homes and no turns."""

import copy
import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import test_sync as fixtures
from agent_sync import audit, codex, config
from agent_sync.files import SyncError, digest, encode
from agent_sync.native import CodexReader

TID = '11111111-1111-4111-8111-111111111111'


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SyncIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.machine = self.fixture.a
        self.rel, self.paginated = self.fixture.seed_paginated(self.machine, TID)
        self.rows = [json.loads(line) for line in self.paginated.splitlines()]
        self.rows[0]['payload'].pop('history_mode')
        for row in self.rows:
            row.pop('ordinal', None)
        self.legacy = fixtures.lines(*self.rows)
        files = {self.rel: (self.paginated, 0)}
        codex.export_history(self.machine / 'codex', files)
        self.exports = {rel: [data] for rel, (data, _) in files.items() if rel.startswith(codex.EXPORT_DIR)}
        self.export_rel = next(iter(self.exports))

    def preserve(self, data=None, rel=None):
        data = self.paginated if data is None else data
        rel = rel or self.rel
        folder = self.machine / 'state/conflicts/codex' / digest(rel.encode()) / digest(data)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'content').write_bytes(data)
        (folder / 'path.txt').write_text(rel + '\n')
        return folder

    def tree(self):
        return {str(p.relative_to(self.fixture.root)): (digest(p.read_bytes()), p.stat().st_mtime_ns)
                for p in self.fixture.root.rglob('*') if p.is_file()}

    def prepare_audit(self):
        self.fixture.command(self.machine, 'push', '--tool', 'codex')
        self.fixture.put(self.machine, 'codex', self.rel, self.legacy)
        self.preserve()

    def test_export_requires_exact_hash_path_identity_and_complete_projection(self):
        matched = audit.matched_export(self.rel, self.paginated, self.exports)
        self.assertEqual(matched['thread_id'], TID)
        for mutation in ('hash', 'path', 'identity', 'projection', 'ambiguous'):
            with self.subTest(mutation=mutation):
                candidate = copy.deepcopy(matched)
                exports = copy.deepcopy(self.exports)
                if mutation == 'hash':
                    candidate['rollout_sha256'] = '0' * 64
                elif mutation == 'path':
                    candidate['rollout_path'] = self.rel.replace('sessions/', 'archived_sessions/')
                elif mutation == 'identity':
                    candidate['thread_id'] = '22222222-2222-4222-8222-222222222222'
                elif mutation == 'projection':
                    candidate['tables']['thread_history_projection_state'][0]['next_rollout_byte_offset'] -= 1
                elif mutation == 'ambiguous':
                    candidate['tables']['thread_turns'][0]['status'] = 'interrupted'
                exports[self.export_rel] = [encode(candidate)]
                if mutation == 'ambiguous':
                    exports[self.export_rel] += self.exports[self.export_rel]
                with self.assertRaises(SyncError):
                    audit.matched_export(self.rel, self.paginated, exports)

    def test_missing_export_is_inconclusive_not_equal(self):
        with mock.patch.object(audit, 'native_turns', return_value=[]):
            result = audit.compare_codex(self.rel, self.legacy, self.paginated, {}, {})
        self.assertEqual(result['classification'], 'inconclusive')
        self.assertIn('checksum-matched', result['reason'])

    def test_order_and_duplicate_tool_results_are_preserved(self):
        call = {'type': 'function_call', 'call_id': 'c1', 'name': 'exec_command', 'arguments': '{"cmd":"pwd"}'}
        output = {'type': 'function_call_output', 'call_id': 'c1', 'output': '/tmp/project'}
        a = [call, output, output]
        self.assertEqual(audit.relation(a, [call, output]), 'local_additional')
        self.assertEqual(audit.relation(a, [output, call, output]), 'divergent')
        changed = copy.deepcopy(a); changed[1]['output'] = '/wrong'
        self.assertEqual(audit.relation(a, changed), 'divergent')

    def test_status_errors_nested_ids_and_command_outputs_survive_normalization(self):
        turn = {'id': 'generated', 'itemsView': 'full', 'status': 'failed', 'error': {'message': 'oops'},
                'items': [{'type': 'commandExecution', 'id': 'call-1', 'aggregatedOutput': 'OK',
                           'status': 'completed', 'exitCode': 0}]}
        clean = audit.normalized_turns([turn])
        self.assertEqual(clean[0]['error'], turn['error'])
        self.assertEqual(clean[0]['items'], turn['items'])
        for key, value in [('status', 'completed'), ('error', None)]:
            changed = copy.deepcopy(turn); changed[key] = value
            self.assertEqual(audit.relation(clean, audit.normalized_turns([changed])), 'divergent')
        changed = copy.deepcopy(turn); changed['items'][0]['aggregatedOutput'] = 'BAD'
        self.assertEqual(audit.relation(clean, audit.normalized_turns([changed])), 'divergent')

    def test_native_reader_missing_commands_cannot_claim_equivalence(self):
        with mock.patch.object(audit, 'native_turns', side_effect=[[], [{'items': [{'type': 'commandExecution'}]}]]):
            result = audit.compare_codex(self.rel, self.legacy, self.paginated, {}, self.exports)
        self.assertEqual(result['classification'], 'inconclusive')
        self.assertIn('Native views differ', result['reason'])

    def test_archival_context_and_undo_are_reported_separately(self):
        rows = copy.deepcopy(self.rows)
        rows[0]['payload']['base_instructions'] = {'text': 'historical instructions'}
        rows += [{'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                  'content': [{'type': 'output_text', 'text': 'Rolled-back reply'}]}},
                 {'type': 'event_msg', 'payload': {'type': 'thread_rolled_back', 'num_turns': 1}},
                 {'type': 'response_item', 'payload': {'type': 'ghost_snapshot', 'ghost_commit': {'id': 'abc'}}}]
        with mock.patch.object(audit, 'native_turns', return_value=[]):
            result = audit.compare_codex(self.rel, fixtures.lines(*rows), self.paginated, {}, self.exports)
        self.assertEqual(result['classification'], 'local_additional_content')
        for key in ('rollback_history', 'instruction_context', 'undo_metadata', 'response_records'):
            self.assertEqual(result['sections'][key]['relation'], 'local_additional')
        undo_only = self.rows + [rows[-1]]
        with mock.patch.object(audit, 'native_turns', return_value=[]):
            result = audit.compare_codex(self.rel, fixtures.lines(*undo_only), self.paginated, {}, self.exports)
        self.assertEqual(result['classification'], 'equivalent_content')
        self.assertEqual(result['sections']['undo_metadata']['relation'], 'local_additional')

    def test_additional_and_divergent_content_both_directions(self):
        extra = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_text', 'text': 'Another question'}]}}
        longer = fixtures.lines(*(self.rows + [extra]))
        with mock.patch.object(audit, 'native_turns', return_value=[]):
            left = audit.compare_codex(self.rel, longer, self.legacy, {}, {})
            right = audit.compare_codex(self.rel, self.legacy, longer, {}, {})
            changed = copy.deepcopy(self.rows)
            changed[1]['payload']['content'][0]['text'] = 'Different question'
            divergence = audit.compare_codex(self.rel, self.legacy, fixtures.lines(*changed), {}, {})
        self.assertEqual(left['classification'], 'local_additional_content')
        self.assertEqual(right['classification'], 'alternative_additional_content')
        self.assertEqual(divergence['classification'], 'divergent_content')

    def test_unknown_records_fail_closed(self):
        unknown = {'type': 'future_format', 'payload': {'new_content': 'keep me'}}
        with mock.patch.object(audit, 'native_turns', return_value=[]):
            result = audit.compare_codex(self.rel, fixtures.lines(*(self.rows + [unknown])), self.legacy, {}, {})
        self.assertEqual(result['classification'], 'inconclusive')

    def test_pagination_reads_every_page_in_ascending_order_and_rejects_loops(self):
        native = mock.Mock()
        native.call.side_effect = [{'data': [1, 2], 'nextCursor': 'c1'}, {'data': [3], 'nextCursor': None}]
        self.assertEqual(audit.read_pages(native, TID), [1, 2, 3])
        self.assertEqual(native.call.call_args_list[1].args[1]['cursor'], 'c1')
        self.assertEqual(native.call.call_args_list[0].args[1]['sortDirection'], 'asc')
        native.call.side_effect = [{'data': [1], 'nextCursor': 'c1'}, {'data': [1], 'nextCursor': 'c1'}]
        with self.assertRaises(SyncError):
            audit.read_pages(native, TID)

    def test_input_change_and_symlink_and_conflict_corruption_rejected(self):
        folder = self.preserve()
        inputs = audit.Inputs()
        inputs.read(folder / 'content')
        (folder / 'content').write_bytes(b'changed')
        with self.assertRaises(SyncError):
            inputs.verify()
        with self.assertRaises(SyncError):
            audit.conflicts(self.machine / 'state', ['codex'], audit.Inputs())
        (folder / 'content').unlink()
        (folder / 'content').symlink_to(self.machine / 'codex' / self.rel)
        with self.assertRaises(SyncError):
            audit.conflicts(self.machine / 'state', ['codex'], audit.Inputs())

    def test_traversal_cannot_escape_conflict_folder(self):
        rel = 'sessions/../../outside.jsonl'
        self.preserve(rel=rel)
        with self.assertRaises(SyncError):
            audit.conflicts(self.machine / 'state', ['codex'], audit.Inputs())

    def test_current_history_reads_only_disposable_sqlite_copy_including_wal(self):
        db_path = self.machine / 'codex' / codex.DB_NAME
        db = sqlite3.connect(str(db_path))
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("UPDATE thread_turns SET status='interrupted'")
        db.commit()
        before = self.tree()
        real_connect = sqlite3.connect
        opened = []
        def connect(path, *args, **kwargs):
            opened.append(str(path))
            self.assertNotIn(str(self.machine), str(path))
            return real_connect(path, *args, **kwargs)
        with mock.patch('sqlite3.connect', side_effect=connect):
            exports = audit.current_exports(self.machine / 'codex', {self.rel: self.paginated}, audit.Inputs())
        self.assertTrue(opened)
        obj = json.loads(exports[self.export_rel][0])
        self.assertEqual(obj['tables']['thread_turns'][0]['status'], 'interrupted')
        self.assertEqual(self.tree(), before)

    def test_empty_audit_does_not_create_lock_or_modify_configuration(self):
        before = self.tree()
        result = self.fixture.command(self.machine, 'audit-conflicts', '--json')
        self.assertEqual(json.loads(result.stdout)['summary']['comparisons'], 0)
        self.assertEqual(self.tree(), before)

    def test_missing_native_cli_reports_inconclusive_without_touching_sources(self):
        self.prepare_audit()
        before = self.tree()
        with mock.patch('agent_sync.native.shutil.which', return_value=None):
            report = audit.build_report(self.machine / 'state', config.load(self.machine / 'state'), ['codex'])
        self.assertEqual(report['conversations'][0]['classification'], 'inconclusive')
        self.assertEqual(self.tree(), before)

    def test_optional_input_disappearance_is_detected(self):
        path = self.machine / 'codex' / self.rel
        inputs = audit.Inputs()
        inputs.read(path, optional=True)
        path.unlink()
        with self.assertRaises(SyncError):
            inputs.read(path, optional=True)

    def test_reader_forbids_turns_and_mutations(self):
        reader = CodexReader(self.machine)
        for method in ('thread/name/set', 'thread/resume', 'turn/start', 'command/exec', 'thread/rollback'):
            with self.assertRaises(ValueError):
                reader.call(method, {})

    def test_omitted_or_orphaned_export_rows_prevent_equivalence(self):
        obj = json.loads(self.exports[self.export_rel][0])
        item = json.loads(obj['tables']['thread_items'][0]['item_json'])
        turns = [{'id': 'turn-1', 'items': [item], 'status': 'completed', 'error': None}]
        audit.validate_native_coverage(obj, turns)
        for mutation in ('missing_turn', 'missing_item', 'orphan', 'realtime', 'changed_output'):
            with self.subTest(mutation=mutation):
                altered, native = copy.deepcopy(obj), copy.deepcopy(turns)
                if mutation == 'missing_turn':
                    native = []
                elif mutation == 'missing_item':
                    native[0]['items'] = []
                elif mutation == 'orphan':
                    row = copy.deepcopy(altered['tables']['thread_items'][0])
                    row['turn_id'] = 'missing-turn'
                    altered['tables']['thread_items'].append(row)
                elif mutation == 'realtime':
                    altered['tables']['thread_realtime_items'] = [{'item_id': 'unread'}]
                else:
                    native[0]['items'][0]['content'][0]['text'] = 'changed'
                with self.assertRaises(SyncError):
                    audit.validate_native_coverage(altered, native)

    def test_multiple_alternatives_group_by_thread_and_report_mixed_results(self):
        self.prepare_audit()
        rows = copy.deepcopy(self.rows)
        rows[0]['payload']['base_instructions'] = {'text': 'additional remote context'}
        self.preserve(fixtures.lines(*rows))
        with mock.patch.object(audit, 'native_turns', return_value=[]):
            report = audit.build_report(self.machine / 'state', config.load(self.machine / 'state'), ['codex'])
        self.assertEqual(report['summary']['conflict_files'], 1)
        self.assertEqual(report['summary']['comparisons'], 2)
        self.assertEqual(report['summary']['conversations'], 1)
        self.assertEqual(report['conversations'][0]['classification'], 'inconclusive')
        self.assertEqual({c['classification'] for c in report['conversations'][0]['comparisons']},
                         {'equivalent_content', 'alternative_additional_content'})

    @unittest.skipUnless(shutil.which('codex'), 'Codex CLI is not installed')
    def test_native_full_pagination_and_command_results(self):
        obj = json.loads(self.exports[self.export_rel][0])
        seed_turn = obj['tables']['thread_turns'][0]
        seed_item = obj['tables']['thread_items'][0]
        obj['tables']['thread_turns'] = []
        obj['tables']['thread_items'] = []
        for i in range(105):
            turn_id, item_id = 'turn-{}'.format(i), 'command-{}'.format(i)
            turn = dict(seed_turn, turn_id=turn_id, rollout_ordinal=i + 1, first_user_item_id=None)
            item = dict(seed_item, turn_id=turn_id, item_id=item_id, rollout_ordinal=i + 1,
                        updated_at_ordinal=i + 1, item_type='commandExecution')
            item['item_json'] = json.dumps({'type': 'commandExecution', 'id': item_id,
                'command': 'printf synthetic', 'cwd': '/synthetic', 'processId': None,
                'status': 'completed', 'commandActions': [], 'aggregatedOutput': 'output {}'.format(i),
                'exitCode': 0, 'durationMs': 1})
            obj['tables']['thread_turns'].append(turn)
            obj['tables']['thread_items'].append(item)
        turns = audit.native_turns(self.rel, self.paginated, obj)
        self.assertEqual(len(turns), 105)
        self.assertEqual([t['items'][0]['aggregatedOutput'] for t in turns],
                         ['output {}'.format(i) for i in range(105)])
        altered = copy.deepcopy(obj)
        row = altered['tables']['thread_items'][-1]
        payload = json.loads(row['item_json']); payload['aggregatedOutput'] = 'different tool result'
        row['item_json'] = json.dumps(payload)
        changed = audit.native_turns(self.rel, self.paginated, altered)
        self.assertEqual(audit.relation(turns, changed), 'divergent')

    @unittest.skipUnless(shutil.which('codex'), 'Codex CLI is not installed')
    def test_native_cli_equivalence_uses_full_content_and_preserves_all_sources(self):
        self.prepare_audit()
        before = self.tree()
        output = self.fixture.command(self.machine, 'audit-conflicts', '--tool', 'codex', '--json')
        report = json.loads(output.stdout)
        group = report['conversations'][0]
        self.assertEqual(group['thread_id'], TID)
        self.assertEqual(group['classification'], 'equivalent_content')
        detail = group['comparisons'][0]
        self.assertEqual(detail['sections']['active_dialogue']['local_count'], 1)
        self.assertEqual(detail['history_rows_loaded']['alternative'], 3)
        self.assertEqual(self.tree(), before)
        self.assertNotIn('Synthetic paginated conversation', output.stdout)

    @unittest.skipUnless(shutil.which('codex'), 'Codex CLI is not installed')
    def test_native_same_rollout_with_changed_history_is_not_equal(self):
        other = copy.deepcopy(self.exports)
        obj = json.loads(other[self.export_rel][0])
        item = obj['tables']['thread_items'][0]
        content = json.loads(item['item_json'])
        content['content'][0]['text'] = 'Different database-only message'
        item['item_json'] = json.dumps(content)
        other[self.export_rel] = [encode(obj)]
        result = audit.compare_codex(self.rel, self.paginated, self.paginated, self.exports, other)
        self.assertEqual(result['classification'], 'inconclusive')
        self.assertEqual(result['sections']['active_dialogue']['relation'], 'divergent')

    @unittest.skipUnless(shutil.which('codex'), 'Codex CLI is not installed')
    def test_native_archived_empty_session_context_is_additional(self):
        local_rows = [copy.deepcopy(self.rows[0])]
        local_rows[0]['payload']['base_instructions'] = {'text': 'archived context'}
        alternative = fixtures.lines(self.rows[0])
        rel = self.rel.replace('sessions/', 'archived_sessions/')
        result = audit.compare_codex(rel, fixtures.lines(*local_rows), alternative, {}, {})
        self.assertEqual(result['classification'], 'local_additional_content')
        self.assertEqual(result['sections']['active_dialogue']['local_count'], 0)
