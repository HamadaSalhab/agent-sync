"""Explicit history schema compatibility; isolated homes and no model turns."""

import copy
import json
import shutil
import sqlite3
import unittest

import test_sync as fixtures
from agent_sync import codex, store
from agent_sync.files import SyncError, collect, encode, merge_file
from agent_sync.native import CodexReader

TID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class CodexSchemaTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SyncIntegrationTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.rel, self.data = self.f.seed_paginated(self.f.a, TID)

    def db(self, machine):
        return sqlite3.connect(str(machine / "codex" / codex.DB_NAME))

    def upgrade(self, machine):
        with self.db(machine) as db:
            for field in codex.ITEM_TIMINGS:
                db.execute('ALTER TABLE thread_items ADD COLUMN "{}" INTEGER'.format(field))

    def exported(self):
        files = collect(self.f.a / "codex", "codex")
        rel = next(p for p in files if p.startswith(codex.EXPORT_DIR))
        return rel, codex.validate_export(rel, files[rel][0])

    def test_new_backup_export_import_and_restore_preserve_timing(self):
        self.upgrade(self.f.a)
        self.f.seed_paginated(self.f.b, OTHER)
        self.upgrade(self.f.b)
        with self.db(self.f.a) as db:
            db.execute("UPDATE thread_items SET started_at_ms=1789516801000, completed_at_ms=1789516802345")
            db.execute("INSERT INTO thread_items SELECT thread_id,turn_id,'null-item',2,created_at_ms,item_json,item_type,2,NULL,NULL FROM thread_items")
        rel, obj = self.exported()
        self.assertEqual(obj["version"], 2)
        self.assertEqual({r["started_at_ms"] for r in obj["tables"]["thread_items"]}, {1789516801000, None})
        self.f.command(self.f.a, "backup", "--all")
        name = self.f.command(self.f.a, "backups").stdout.strip().splitlines()[-1]
        saved = store.backup_files(self.f.a / "state", name, ["codex"])
        self.assertEqual(next(json.loads(data) for _, p, data, _ in saved if p == rel), obj)
        self.f.command(self.f.a, "push", "--all")
        self.f.command(self.f.b, "pull", "--all")
        self.f.command(self.f.b, "pull", "--all")
        with self.db(self.f.b) as db:
            self.assertEqual(db.execute("SELECT started_at_ms,completed_at_ms FROM thread_items WHERE thread_id=? ORDER BY item_id", (TID,)).fetchall(),
                             [(1789516801000, 1789516802345), (None, None)])
            self.assertEqual(db.execute("SELECT count(*) FROM thread_items WHERE thread_id=?", (OTHER,)).fetchone()[0], 1)
        with self.db(self.f.a) as db:
            db.execute("DELETE FROM thread_items")
        self.f.command(self.f.a, "restore", name, "--all")
        self.assertEqual(self.exported()[1], obj)

    def test_legacy_export_imports_null_timings_into_new_schema(self):
        rel, obj = self.exported()
        self.assertEqual(obj["version"], 1)
        self.assertNotIn("started_at_ms", obj["tables"]["thread_items"][0])
        self.f.seed_paginated(self.f.b, OTHER)
        self.upgrade(self.f.b)
        codex.prepare_import(self.f.b / "codex", [obj])
        codex.import_history(self.f.b / "codex", [obj])
        with self.db(self.f.b) as db:
            self.assertEqual(db.execute("SELECT started_at_ms,completed_at_ms FROM thread_items WHERE thread_id=?", (TID,)).fetchone(), (None, None))
            self.assertEqual(db.execute("SELECT count(*) FROM thread_items WHERE thread_id=?", (OTHER,)).fetchone()[0], 1)

    def test_equivalent_legacy_and_new_exports_do_not_conflict(self):
        from agent_sync import audit
        rel, old = self.exported()
        self.upgrade(self.f.a)
        _, new = self.exported()
        legacy, current = (encode(old), 200), (encode(new), 100)
        for local, remote in ((legacy, current), (current, legacy), (None, legacy)):
            chosen, stamp, alternatives = merge_file("codex", rel, [current, remote], local)
            self.assertEqual((chosen, stamp, alternatives), (*current, []))
        self.assertEqual(audit.matched_export(self.rel, self.data, {rel: [legacy[0], current[0]]}), old)
        self.f.seed_paginated(self.f.b, OTHER)
        self.f.command(self.f.b, "push", "--tool", "codex")
        self.f.command(self.f.a, "pull", "--tool", "codex")
        self.f.command(self.f.a, "push", "--tool", "codex")
        self.upgrade(self.f.b)
        self.f.command(self.f.b, "pull", "--tool", "codex")
        self.f.command(self.f.b, "pull", "--tool", "codex")

    def test_equivalence_never_discards_nonnull_timings_or_other_differences(self):
        rel, old = self.exported()
        self.upgrade(self.f.a)
        _, new = self.exported()
        for mutation in ("timing", "content", "metadata"):
            changed = copy.deepcopy(new)
            if mutation == "timing":
                changed["tables"]["thread_items"][0]["started_at_ms"] = 1789516801000
            elif mutation == "content":
                changed["tables"]["thread_items"][0]["item_json"] = '{}'
            else:
                changed["extra_metadata"] = "retained"
            legacy, current = (encode(old), 200), (encode(changed), 100)
            for local, remote in ((legacy, current), (current, legacy)):
                with self.subTest(mutation=mutation, local_version=json.loads(local[0])["version"]):
                    chosen, stamp, alternatives = merge_file("codex", rel, [remote], local)
                    self.assertEqual((chosen, stamp, alternatives), (*local, [remote]))

    def test_new_export_refuses_old_destination_before_any_changes(self):
        _, old = self.exported()
        self.upgrade(self.f.a)
        _, new = self.exported()
        self.f.seed_paginated(self.f.b, OTHER)
        path = self.f.b / "codex" / codex.DB_NAME
        before = path.read_bytes()
        for operation in (codex.prepare_import, codex.import_history):
            with self.assertRaisesRegex(SyncError, "Upgrade Codex"):
                operation(self.f.b / "codex", [old, new])
            self.assertEqual(path.read_bytes(), before)
        self.f.command(self.f.a, "push", "--all")
        result = self.f.command(self.f.b, "pull", "--all", code=1)
        self.assertIn("Upgrade Codex", result.stderr)
        self.assertFalse((self.f.b / "codex" / self.rel).exists())
        self.assertEqual(path.read_bytes(), before)

    def test_unknown_partial_and_incompatible_schema_fail_closed(self):
        with self.db(self.f.a) as db:
            ddl = list(db.execute("SELECT sql FROM sqlite_master WHERE type='table'"))
        for columns in (("surprise TEXT",), ("started_at_ms INTEGER",),
                        ("completed_at_ms INTEGER",),
                        ("started_at_ms TEXT", "completed_at_ms INTEGER"),
                        ("started_at_ms INTEGER NOT NULL DEFAULT 0", "completed_at_ms INTEGER"),
                        ("started_at_ms INTEGER DEFAULT 0", "completed_at_ms INTEGER"),
                        ("started_at_ms INTEGER", "completed_at_ms INTEGER", "surprise TEXT")):
            with self.subTest(columns=columns), sqlite3.connect(":memory:") as db:
                for (sql,) in ddl:
                    db.execute(sql)
                for column in columns:
                    db.execute("ALTER TABLE thread_items ADD COLUMN " + column)
                with self.assertRaisesRegex(SyncError, "Unsupported Codex history schema"):
                    codex.check_schema(db)

    def test_unknown_schema_backup_fails_without_partial_output(self):
        self.upgrade(self.f.a)
        with self.db(self.f.a) as db:
            db.execute("ALTER TABLE thread_items ADD COLUMN surprise TEXT")
        result = self.f.command(self.f.a, "backup", "--all", code=1)
        self.assertIn("surprise", result.stderr)
        self.assertFalse((self.f.a / "state/backups").exists())

    def test_export_versions_and_timestamp_values_are_strict(self):
        self.upgrade(self.f.a)
        rel, obj = self.exported()
        for value in (True, False, 1.5, "1000", [], {}, 2 ** 63, -(2 ** 63) - 1):
            for field in codex.ITEM_TIMINGS:
                bad = copy.deepcopy(obj)
                bad["tables"]["thread_items"][0][field] = value
                with self.subTest(value=value, field=field), self.assertRaises(SyncError):
                    codex.validate_export(rel, encode(bad))
        for value in (None, 0, -1, 2 ** 63 - 1, -(2 ** 63)):
            good = copy.deepcopy(obj)
            good["tables"]["thread_items"][0]["started_at_ms"] = value
            self.assertEqual(codex.validate_export(rel, encode(good)), good)
        for mutation in ("missing", "extra", "v1", "unknown", "bool", "float"):
            bad = copy.deepcopy(obj)
            row = bad["tables"]["thread_items"][0]
            if mutation == "missing":
                row.pop("completed_at_ms")
            elif mutation == "extra":
                row["surprise"] = None
            else:
                bad["version"] = {"v1": 1, "unknown": 3, "bool": True, "float": 2.0}[mutation]
            with self.subTest(mutation=mutation), self.assertRaises(SyncError):
                codex.validate_export(rel, encode(bad))

    def test_constraint_failure_rolls_back_all_imported_threads(self):
        self.upgrade(self.f.a)
        _, obj = self.exported()
        self.f.seed_paginated(self.f.b, OTHER)
        self.upgrade(self.f.b)
        bad = copy.deepcopy(obj)
        bad["tables"]["thread_items"].append(copy.deepcopy(bad["tables"]["thread_items"][0]))
        path = self.f.b / "codex" / codex.DB_NAME
        before = path.read_bytes()
        for operation in (codex.prepare_import, codex.import_history):
            with self.assertRaises(SyncError):
                operation(self.f.b / "codex", [bad])
            self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is not installed")
    def test_native_schema_bootstrap_and_round_trip_without_turns(self):
        root = self.f.b / "codex"
        codex.bootstrap(root)
        with self.db(self.f.b) as db:
            version = codex.check_schema(db)
        if version == 2:
            self.upgrade(self.f.a)
            with self.db(self.f.a) as db:
                db.execute("UPDATE thread_items SET started_at_ms=1789516801000, completed_at_ms=1789516802345")
        _, obj = self.exported()
        self.f.command(self.f.a, "push", "--all")
        self.f.command(self.f.b, "pull", "--all")
        with CodexReader(root, isolated=True) as native:
            page = native.call("thread/turns/list", {"threadId": TID, "limit": 10, "itemsView": "full"})
            self.assertIn("Synthetic paginated conversation", json.dumps(page["data"]))
        files = collect(root, "codex")
        exported = next(json.loads(data) for rel, (data, _) in files.items() if rel.startswith(codex.EXPORT_DIR))
        self.assertEqual(exported, obj)
