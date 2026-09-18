"""Synthetic transport/recovery tests; no real homes, remotes, or model turns."""

import copy
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import test_sync as fixtures
from agent_sync import audit, cli, codex, config, snapshot, store
from agent_sync.files import SyncError, collect, digest, encode


class LargeFileTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SyncIntegrationTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.repo = self.f.a / 'state/repository'
        self.cfg = config.load(self.f.a / 'state')
        self.root = self.repo / 'machines' / self.cfg['machine_id'] / 'claude'

    def seed(self, data=None):
        data = bytes(range(256)) * 40 if data is None else data
        rel = 'file-history/project/file'
        self.f.put(self.f.a, 'claude', rel, data)
        return rel, data

    def snapshot(self, chunk_size=2048):
        store.refresh(self.repo, self.cfg)
        with mock.patch.object(snapshot, 'CHUNK_SIZE', chunk_size):
            store.write_snapshot(self.repo, self.cfg, 'claude', collect(self.f.a / 'claude', 'claude'))
        store.commit(self.repo, 'Synthetic snapshot')

    def test_chunked_push_pull_exact_bytes_mtime_and_idempotence(self):
        # Exercise the production boundary through the real subprocess CLI.
        rel, data = self.seed(b'\x00synthetic\xff' * (snapshot.CHUNK_SIZE // 11 + 2))
        self.f.command(self.f.a, 'push', '--tool', 'claude')
        meta = json.loads((self.root / 'manifest.json').read_text())
        self.assertEqual(meta['version'], 2)
        self.assertGreater(len(meta['files'][rel]['chunks']), 1)
        self.assertFalse((self.root / 'data' / rel).exists())
        self.f.command(self.f.b, 'pull', '--tool', 'claude')
        target = self.f.b / 'claude' / rel
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(target.stat().st_mtime_ns, (self.f.a / 'claude' / rel).stat().st_mtime_ns)
        before = store.git(self.repo, 'rev-parse', 'HEAD').stdout
        self.f.command(self.f.a, 'push', '--tool', 'claude')
        self.assertEqual(store.git(self.repo, 'rev-parse', 'HEAD').stdout, before)
        # A clean new machine can initialize directly from a v2 remote.
        third = self.f.machine('third')
        self.f.command(third, 'pull', '--tool', 'claude')
        self.assertEqual((third / 'claude' / rel).read_bytes(), data)

    def test_mixed_legacy_and_chunked_snapshots_and_old_client_gate(self):
        rel, data = self.seed()
        self.f.put(self.f.b, 'codex', 'sessions/old.jsonl', fixtures.lines({'legacy': True}))
        self.f.command(self.f.b, 'push', '--tool', 'codex')
        store.refresh(self.repo, self.cfg)
        self.snapshot()
        result = store.read_snapshots(self.repo, ['claude', 'codex'])
        self.assertEqual(result['claude'][rel][0][0], data)
        self.assertIn('sessions/old.jsonl', result['codex'])
        marker = json.loads((self.repo / 'agent-sync.json').read_text())
        self.assertEqual(marker['version'], 2)
        # The old client's exact version-1 format check cannot accept this.
        self.assertTrue(any(marker.get(k) != v for k, v in store.FORMAT.items()))

    def test_corrupt_missing_reordered_or_malicious_chunks_rejected(self):
        rel, data = self.seed(b'a' * 2048 + b'b' * 2048 + b'c' * 123)
        self.snapshot()
        original = json.loads((self.root / 'manifest.json').read_text())['files'][rel]
        for mutation in ('checksum', 'missing', 'order', 'size', 'path', 'bool', 'empty', 'symlink', 'v1'):
            with self.subTest(mutation=mutation):
                attrs = copy.deepcopy(original)
                version = 2
                saved = None
                if mutation == 'checksum': attrs['sha256'] = '0' * 64
                elif mutation == 'missing': attrs['chunks'][0]['sha256'] = '0' * 64
                elif mutation == 'order': attrs['chunks'].reverse()
                elif mutation == 'size': attrs['size'] += 1
                elif mutation == 'path': attrs['chunks'][0]['sha256'] = '../outside'
                elif mutation == 'bool': attrs['chunks'][0]['size'] = True
                elif mutation == 'empty': attrs['chunks'] = []
                elif mutation == 'v1': version = 1
                elif mutation == 'symlink':
                    path = self.root / 'chunks' / attrs['chunks'][0]['sha256']
                    saved = path.read_bytes()
                    path.unlink()
                    outside = self.f.root / 'outside'
                    outside.write_bytes(saved)
                    path.symlink_to(outside)
                try:
                    with self.assertRaises(SyncError):
                        snapshot.read_file(self.root, rel, attrs, version)
                finally:
                    if saved is not None:
                        path.unlink()
                        path.write_bytes(saved)

    def test_tampered_chunk_stops_pull_before_local_changes(self):
        rel, data = self.seed()
        self.snapshot()
        chunk = next((self.root / 'chunks').iterdir())
        chunk.write_bytes(b'tampered')
        store.commit(self.repo, 'Corrupt test fixture')
        store.git(self.repo, 'push', 'origin', 'HEAD:main')
        result = self.f.command(self.f.b, 'pull', '--tool', 'claude', code=1)
        self.assertIn('chunk checksum', result.stderr)
        self.assertFalse((self.f.b / 'claude' / rel).exists())
        self.assertFalse((self.f.b / 'state/backups').exists())

    def test_chunked_conflicts_and_backups_preserve_complete_files(self):
        rel = 'projects/p/session.jsonl'
        remote, local = fixtures.lines({'message': 'remote' * 1000}), fixtures.lines({'message': 'local'})
        self.f.put(self.f.a, 'claude', rel, remote)
        self.f.put(self.f.b, 'claude', rel, local)
        self.snapshot()
        store.git(self.repo, 'push', 'origin', 'HEAD:main')
        self.f.command(self.f.b, 'pull', '--tool', 'claude', code=2)
        self.assertEqual((self.f.b / 'claude' / rel).read_bytes(), local)
        copies = list((self.f.b / 'state/conflicts').rglob('content'))
        self.assertEqual([p.read_bytes() for p in copies], [remote])
        backup = next((self.f.b / 'state/backups').iterdir())
        self.assertEqual(store.backup_files(self.f.b / 'state', backup.name, ['claude'])[0][2], local)

    def test_repair_rejected_snapshot_preserves_original_and_ignores_live_data(self):
        rel, data = self.seed()
        self.snapshot(chunk_size=100000)
        old = store.git(self.repo, 'rev-parse', 'HEAD').stdout.strip()
        self.f.put(self.f.a, 'claude', rel, b'new unsnapshotted work')
        cfg_bytes = (self.f.a / 'state/config.json').read_bytes()
        # Simulate a remote enforcing a small per-blob limit for this fixture.
        hook = self.f.remote / 'hooks/pre-receive'
        hook.write_text('#!/usr/bin/env python3\nimport sys,subprocess\n'
                        'old,new,ref=sys.stdin.readline().split()\n'
                        'ids=subprocess.check_output(["git","rev-list","--objects",new,"^"+old]).splitlines()\n'
                        'for line in ids:\n'
                        ' oid=line.split()[0].decode()\n'
                        ' kind=subprocess.check_output(["git","cat-file","-t",oid]).strip()\n'
                        ' size=int(subprocess.check_output(["git","cat-file","-s",oid]))\n'
                        ' if kind==b"blob" and size>4096: sys.exit(1)\n')
        hook.chmod(0o700)
        self.assertNotEqual(store.git(self.repo, 'push', 'origin', 'HEAD:main', check=False).returncode, 0)
        with mock.patch.object(snapshot, 'CHUNK_SIZE', 2048), mock.patch.object(store, 'GIT_BLOB_LIMIT', 4096):
            with self.assertRaisesRegex(SyncError, 'repair-large-files'):
                store.check_push_size(self.repo)
            args = cli.parser().parse_args(['repair-large-files', '--push'])
            cli.run(args, self.f.a / 'state')
        refs = store.git(self.repo, 'for-each-ref', '--format=%(objectname)', 'refs/agent-sync/recovery/').stdout.split()
        self.assertEqual(refs, [old])
        path = 'machines/' + self.cfg['machine_id'] + '/claude/data/' + rel
        original = subprocess.check_output(['git', '-C', str(self.repo), 'show', old + ':' + path])
        self.assertEqual(original, data)
        self.assertEqual((self.f.a / 'claude' / rel).read_bytes(), b'new unsnapshotted work')
        self.assertEqual((self.f.a / 'state/config.json').read_bytes(), cfg_bytes)
        self.f.command(self.f.b, 'pull', '--tool', 'claude')
        self.assertEqual((self.f.b / 'claude' / rel).read_bytes(), data)

    def test_repair_refuses_published_multiple_or_other_machine_commits(self):
        self.seed()
        self.snapshot(chunk_size=100000)
        with mock.patch.object(store, 'GIT_BLOB_LIMIT', 4096):
            other = self.repo / 'unrelated'
            other.write_text('unrelated')
            store.git(self.repo, 'add', '--all')
            store.git(self.repo, '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                      'commit', '--amend', '--no-edit')
            with self.assertRaisesRegex(SyncError, 'another machine or unrelated'):
                store.repair_large_files(self.repo, self.cfg)
            other.write_text('second commit')
            store.commit(self.repo, 'Second pending commit')
            with self.assertRaisesRegex(SyncError, 'exactly one'):
                store.repair_large_files(self.repo, self.cfg)
            store.git(self.repo, 'push', 'origin', 'HEAD:main')
            with self.assertRaisesRegex(SyncError, 'exactly one'):
                store.repair_large_files(self.repo, self.cfg)
        self.assertFalse(store.git(self.repo, 'for-each-ref', 'refs/agent-sync/recovery/').stdout)

    @unittest.skipUnless(shutil.which('codex'), 'Codex CLI is not installed')
    def test_chunked_paginated_export_native_import_and_readonly_audit(self):
        tid = '11111111-1111-4111-8111-111111111111'
        rel, data = self.f.seed_paginated(self.f.a, tid)
        with mock.patch.object(snapshot, 'CHUNK_SIZE', 128):
            store.write_snapshot(self.repo, self.cfg, 'codex', collect(self.f.a / 'codex', 'codex'))
        store.commit(self.repo, 'Chunked paginated fixture')
        inputs = audit.Inputs()
        exports = audit.cached_exports(self.f.a / 'state', inputs)
        self.assertTrue(exports)
        inputs.verify()
        self.assertTrue(any('/chunks/' in str(path) for path in inputs.files))
        store.git(self.repo, 'push', 'origin', 'HEAD:main')
        self.f.command(self.f.b, 'pull', '--tool', 'codex')
        self.assertEqual((self.f.b / 'codex' / rel).read_bytes(), data)
        with audit.CodexReader(self.f.b / 'codex', isolated=True) as native:
            native.call('thread/read', {'threadId': tid, 'includeTurns': False})
            turns = audit.read_pages(native, tid)
            audit.validate_native_coverage(json.loads(next(iter(exports.values()))[0]), turns)

    @unittest.skipUnless(shutil.which('git-crypt'), 'git-crypt is not installed')
    def test_chunks_remain_encrypted(self):
        remote = self.f.root / 'encrypted.git'
        subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
        c, d, key = self.f.root / 'c', self.f.root / 'd', self.f.root / 'key'
        for machine, extra in ((c, ['--encrypt']), (d, [])):
            self.f.command(machine, 'init', '--remote', str(remote), '--key-file', str(key),
                           '--claude-dir', str(machine / 'claude'), '--codex-dir', str(machine / 'codex'), *extra)
        rel, data = 'file-history/p/file', b'Synthetic encrypted content' * 400
        self.f.put(c, 'claude', rel, data)
        repo, cfg = c / 'state/repository', config.load(c / 'state')
        with mock.patch.object(snapshot, 'CHUNK_SIZE', 2048):
            store.write_snapshot(repo, cfg, 'claude', collect(c / 'claude', 'claude'))
        store.commit(repo, 'Encrypted chunks')
        store.git(repo, 'push', 'origin', 'HEAD:main')
        paths = store.git(repo, 'ls-files').stdout.splitlines()
        for path in [p for p in paths if '/chunks/' in p]:
            raw = subprocess.check_output(['git', '--git-dir', str(remote), 'show', 'main:' + path])
            self.assertTrue(raw.startswith(b'\x00GITCRYPT'))
        self.f.command(d, 'pull', '--tool', 'claude')
        self.assertEqual((d / 'claude' / rel).read_bytes(), data)
