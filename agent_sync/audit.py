"""Read-only reports for preserved conflicts; no resolution or deletion policy.

Native readers only see disposable copies. Equivalence is scoped to the compared
payloads, never permission to replace logs or discard recovery metadata.
"""

import json
import sqlite3
import tempfile
import uuid
from collections import Counter
from pathlib import Path

from . import codex
from .files import SyncError, allowed, digest, json_rows, read_stable, safe_path, transcript
from .native import CodexReader
from .audit_semantics import instruction_records, completed_event_coverage


class Inputs:
    """Track content and mtimes, including missing inputs, across the whole audit."""

    def __init__(self):
        self.files = {}

    def read(self, path, optional=False):
        if path.is_symlink():
            raise SyncError("Symlink audit input: {}".format(path))
        if optional and not path.exists():
            if self.files.get(path) is not None:
                raise SyncError("Audit input disappeared: {}".format(path))
            self.files[path] = None
            return None
        data, stamp = read_stable(path)
        mark = (digest(data), stamp)
        if path in self.files and self.files[path] != mark:
            raise SyncError("Audit input changed: {}".format(path))
        self.files[path] = mark
        return data

    def verify(self):
        for path, mark in self.files.items():
            if mark is None:
                if path.exists():
                    raise SyncError("Audit input appeared during comparison: {}".format(path))
            else:
                data, stamp = read_stable(path)
                if (digest(data), stamp) != mark:
                    raise SyncError("Audit input changed during comparison: {}".format(path))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def relation(local, alternative):
    """Ordered subsequence comparison retains duplicates and tool-result ordering."""
    if local == alternative:
        return "equal"

    def contains(longer, shorter):
        cursor = iter(longer)
        return all(any(candidate == item for candidate in cursor) for item in shorter)

    if contains(local, alternative):
        return "local_additional"
    if contains(alternative, local):
        return "alternative_additional"
    return "divergent"


def evidence(local, alternative):
    return {"relation": relation(local, alternative), "local_count": len(local),
            "alternative_count": len(alternative),
            "local_sha256": digest(canonical(local).encode()),
            "alternative_sha256": digest(canonical(alternative).encode())}


def unknown_evidence(local, alternative):
    info = evidence(local, alternative)
    for side, records in [('local', local), ('alternative', alternative)]:
        info[side + '_types'] = dict(Counter(
            r['type'] + '/' + str(r['payload'].get('type', '(no subtype)')) for r in records))
    return info


def rollout(data):
    rows = json_rows(data, "audit rollout")
    if not rows or rows[0].get("type") != "session_meta":
        raise SyncError("Codex rollout has no session_meta header")
    meta = rows[0].get("payload")
    if not isinstance(meta, dict):
        raise SyncError("Invalid Codex session metadata")
    try:
        thread_id = str(uuid.UUID(meta["id"]))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SyncError("Codex rollout has no valid thread UUID")
    mode = meta.get("history_mode", "legacy")
    if mode not in ("legacy", "paginated"):
        raise SyncError("Unsupported Codex history mode: {}".format(mode))
    return thread_id, mode, rows


def raw_sections(rows):
    sections = {key: [] for key in ("response_records", "rollback_history", "instruction_context",
                                   "undo_metadata", "other_metadata", "unrecognized_records")}
    # These events are represented by full native turns. Unknown events remain
    # visible and prevent a claim of equivalence if they differ.
    native_events = {"user_message", "agent_message", "agent_reasoning", "agent_reasoning_raw_content",
                     "task_started", "task_complete", "turn_started", "turn_complete", "turn_aborted",
                     "token_count", "exec_command_begin", "exec_command_end", "exec_command_output_delta"}
    storage_keys = {"id", "timestamp", "cli_version", "originator", "history_mode"}
    for row in rows:
        kind, payload = row.get("type"), row.get("payload")
        if not isinstance(payload, dict):
            raise SyncError("Unsupported non-object rollout payload")
        subtype = str(payload.get("type", ""))
        if "ghost_snapshot" in subtype or "undo" in subtype:
            section = "undo_metadata"
        elif "rollback" in subtype or "rolled_back" in subtype:
            section = "rollback_history"
        elif kind == "session_meta":
            instructions = {k: v for k, v in payload.items() if "instruction" in k}
            if instructions:
                sections["instruction_context"].append(instructions)
            sections["other_metadata"].append({k: v for k, v in payload.items()
                                                if k not in storage_keys and k not in instructions})
            continue
        elif kind == "turn_context":
            section = "instruction_context"
        elif kind == "response_item":
            section = "response_records"
        elif kind == "event_msg" and subtype in native_events:
            continue
        else:
            section = "unrecognized_records"
        sections[section].append({"type": kind, "payload": payload})
    return sections


def normalized_turns(turns):
    result = []
    for turn in turns:
        if not isinstance(turn, dict) or not isinstance(turn.get("items"), list):
            raise SyncError("Native reader returned an incomplete turn")
        if turn.get("itemsView", "full") != "full":
            raise SyncError("Native reader did not return full items")
        # Generated presentation IDs and timing are not dialogue. Everything
        # else, including status/error, command IDs, outputs and nested IDs, stays.
        clean = {k: v for k, v in turn.items()
                 if k not in ("id", "items", "itemsView", "startedAt", "completedAt", "durationMs")}
        clean["items"] = []
        for item in turn["items"]:
            if not isinstance(item, dict) or "type" not in item:
                raise SyncError("Invalid native conversation item")
            fields = dict(item)
            if fields["type"] in ("userMessage", "agentMessage", "reasoning"):
                fields.pop("id", None)
            clean["items"].append(fields)
        result.append(clean)
    return result


def read_pages(native, thread_id):
    turns, seen = [], set()
    cursor = None
    while True:
        params = {"threadId": thread_id, "limit": 100, "itemsView": "full", "sortDirection": "asc"}
        if cursor is not None:
            params["cursor"] = cursor
        page = native.call("thread/turns/list", params)
        if not isinstance(page.get("data"), list):
            raise SyncError("Invalid native turn page")
        turns.extend(page["data"])
        cursor = page.get("nextCursor")
        if cursor is None:
            return turns
        if not isinstance(cursor, str) or cursor in seen or not page["data"]:
            raise SyncError("Native pagination did not advance")
        seen.add(cursor)


def validate_native_coverage(export, turns):
    """Refuse equivalence if the native view omits persisted turns/items."""
    tables = export["tables"]
    if tables["thread_realtime_items"]:
        raise SyncError("Realtime history rows are retained but not covered by this audit reader")
    stored_turns = sorted(tables["thread_turns"], key=lambda row: row["rollout_ordinal"])
    if [row["turn_id"] for row in stored_turns] != [turn["id"] for turn in turns]:
        raise SyncError("Native turns do not cover the ordered exported history")
    covered = 0

    def includes(native, stored):
        if isinstance(stored, dict):
            return isinstance(native, dict) and all(k in native and includes(native[k], v) for k, v in stored.items())
        if isinstance(stored, list):
            return isinstance(native, list) and len(native) == len(stored) and all(
                includes(a, b) for a, b in zip(native, stored))
        return type(native) is type(stored) and native == stored

    for stored, turn in zip(stored_turns, turns):
        error = json.loads(stored["error_json"]) if stored["error_json"] is not None else None
        if stored["status"] != turn.get("status") or error != turn.get("error"):
            raise SyncError("Native turn status/error differs from its export")
        items = sorted((row for row in tables["thread_items"] if row["turn_id"] == stored["turn_id"]),
                       key=lambda row: (row["rollout_ordinal"], row["item_id"]))
        if [row["item_id"] for row in items] != [item["id"] for item in turn["items"]]:
            raise SyncError("Native items do not cover the ordered exported history")
        for row, item in zip(items, turn["items"]):
            payload = json.loads(row["item_json"])
            if payload.get("type") != row["item_type"] or not includes(item, payload):
                raise SyncError("Native item content differs from its export")
        covered += len(items)
    if covered != len(tables["thread_items"]):
        raise SyncError("Export contains orphaned or unread history items")


def native_turns(rel, data, export=None):
    thread_id, mode, _ = rollout(data)
    with tempfile.TemporaryDirectory(prefix="agent-sync-audit-native-") as tmp:
        home = Path(tmp)
        target = safe_path(home, rel)
        target.parent.mkdir(parents=True)
        target.write_bytes(data)
        if mode == "paginated":
            if export is None:
                raise SyncError("Missing checksum-matched paginated export")
            codex.prepare_import(home, [export])
            codex.import_history(home, [export])
        with CodexReader(home, isolated=True) as native:
            thread = native.call("thread/read", {"threadId": thread_id,
                                                 "includeTurns": mode != "paginated"})["thread"]
            if thread.get("id") != thread_id:
                raise SyncError("Native reader returned a different conversation")
            turns = read_pages(native, thread_id) if mode == "paginated" else thread["turns"]
        if export is not None:
            validate_native_coverage(export, turns)
        return normalized_turns(turns)


def matched_export(rel, data, candidates):
    thread_id, mode, _ = rollout(data)
    if mode != "paginated":
        return None
    export_rel = "{}/{}/{}.json".format(codex.EXPORT_DIR, thread_id, digest(data))
    matches = []
    for candidate in candidates.get(export_rel, []):
        obj = codex.validate_export(export_rel, candidate)
        if obj["rollout_path"] != rel:
            continue
        projection = obj["tables"]["thread_history_projection_state"][0]
        if projection["next_rollout_byte_offset"] != len(data):
            raise SyncError("Paginated export does not cover the exact complete rollout")
        if obj not in matches:
            matches.append(obj)
    if len(matches) != 1:
        raise SyncError("{} checksum-matched exports found; exactly one is required".format(len(matches)))
    return matches[0]


def compare_codex(rel, local, alternative, local_exports, alternative_exports):
    local_id, local_mode, local_rows = rollout(local)
    other_id, other_mode, other_rows = rollout(alternative)
    if local_id != other_id:
        raise SyncError("Conflict versions have different conversation IDs")
    a, b = raw_sections(local_rows), raw_sections(other_rows)
    sections = {key: evidence(a[key], b[key]) for key in a}
    sections['unrecognized_records'] = unknown_evidence(a['unrecognized_records'], b['unrecognized_records'])
    instructions_a, covered_a = instruction_records(local_rows, other_rows)
    instructions_b, covered_b = instruction_records(other_rows, local_rows)
    sections['instruction_storage'] = sections['instruction_context']
    sections['instruction_context'] = evidence(instructions_a, instructions_b)
    sections['instruction_context']['covered_elsewhere'] = evidence(covered_a, covered_b)
    sections['session_instructions'] = evidence(
        [r for r in instructions_a if r.get('scope') == 'session'],
        [r for r in instructions_b if r.get('scope') == 'session'])
    sections['session_instructions']['covered_elsewhere'] = evidence(covered_a, covered_b)
    contexts_a = [r for r in instructions_a if r.get('type') == 'turn_context']
    contexts_b = [r for r in instructions_b if r.get('type') == 'turn_context']
    sections['turn_context'] = evidence(contexts_a, contexts_b)
    # Field-level evidence explains richer historical context without weakening
    # the ordered comparison of complete context records used for classification.
    fields = sorted({key for r in contexts_a + contexts_b for key in r['payload']})
    sections['turn_context']['fields'] = {key: evidence(
        [r['payload'][key] for r in contexts_a if key in r['payload']],
        [r['payload'][key] for r in contexts_b if key in r['payload']]) for key in fields}
    for side, records in [('local', instructions_a), ('alternative', instructions_b)]:
        sections['instruction_context'][side + '_session_instruction_characters'] = sum(
            len(record['value']) for record in records
            if record.get('scope') == 'session' and isinstance(record.get('value'), str))
    result = {"thread_id": local_id, "local_format": local_mode, "alternative_format": other_mode,
              "sections": sections}
    result['findings'] = {key: sections[key]['relation'] for key in
                          ('response_records', 'rollback_history', 'instruction_context',
                           'session_instructions', 'turn_context', 'undo_metadata')
                          if sections[key]['relation'] != 'equal'}
    try:
        left_export = matched_export(rel, local, local_exports)
        right_export = matched_export(rel, alternative, alternative_exports)
        left = native_turns(rel, local, left_export)
        right = native_turns(rel, alternative, right_export)
        sections["active_dialogue"] = evidence(left, right)
        migration = {}
        for side, rows, raw, export in [('local', local_rows, a, left_export),
                                        ('alternative', other_rows, b, right_export)]:
            covered, unknown = completed_event_coverage(rows, local_id, export)
            covered_records = [{"type": r['type'], "payload": r['payload']} for r in covered]
            for record in covered_records:
                raw['unrecognized_records'].remove(record)
            migration[side] = {'covered_count': len(covered), 'uncovered_count': len(unknown),
                               'uncovered_reasons': dict(Counter(reason for _, reason in unknown)),
                               'covered_sha256': digest(canonical(covered_records).encode())}
        result['migration_events'] = migration
        sections['unrecognized_records'] = unknown_evidence(a['unrecognized_records'], b['unrecognized_records'])
        # Raw Responses records preserve tool calls/results and rolled-back
        # material that native readers may omit. Never ignore discrepancies.
        important = [sections[k]["relation"] for k in
                     ("active_dialogue", "response_records", "rollback_history", "instruction_context")]
        non_equal = set(important) - {"equal"}
        if not non_equal:
            result["classification"] = "equivalent_content"
        elif non_equal == {"local_additional"}:
            result["classification"] = "local_additional_content"
        elif non_equal == {"alternative_additional"}:
            result["classification"] = "alternative_additional_content"
        else:
            result["classification"] = "divergent_content"
        result['known_content_classification'] = result['classification']
        if sections["unrecognized_records"]["relation"] != "equal":
            raise SyncError("Unrecognized stored records differ (see types and migration event coverage)")
        if sections["active_dialogue"]["relation"] != "equal" and sections["response_records"]["relation"] == "equal":
            raise SyncError("Native views differ despite matching raw response records; reader coverage is inconclusive")
        result["history_rows_loaded"] = {
            side: sum(len(rows) for rows in obj["tables"].values()) if obj else 0
            for side, obj in (("local", left_export), ("alternative", right_export))}
    except (SyncError, KeyError, TypeError, ValueError, sqlite3.Error) as exc:
        result.update(classification="inconclusive", reason=str(exc))
    return result


def conflicts(state, tools, inputs):
    found = []
    for tool in tools:
        root = safe_path(state, "conflicts/" + tool)
        for folder in sorted(root.glob("*/*")):
            folder = safe_path(state, folder.relative_to(state).as_posix())
            if not folder.is_dir():
                raise SyncError("Invalid conflict directory: {}".format(folder))
            path_file = safe_path(folder, "path.txt")
            rel = inputs.read(path_file).decode().rstrip("\n")
            if not allowed(tool, rel) or folder.parent.name != digest(rel.encode()):
                raise SyncError("Invalid conflict path: {}".format(path_file))
            # Validate relative path before any source access.
            safe_path(Path("/audit-placeholder"), rel)
            content = inputs.read(safe_path(folder, "content"))
            if digest(content) != folder.name:
                raise SyncError("Conflict checksum mismatch: {}".format(folder))
            found.append((tool, rel, content, str(folder)))
    return found


def cached_exports(state, inputs):
    """Read only portable exports from the existing v1 snapshots, never fetch."""
    repo = state / "repository"
    marker = json.loads(inputs.read(safe_path(repo, "agent-sync.json")))
    if marker.get("format") != "agent-sync" or marker.get("version") != 1:
        raise SyncError("Unsupported cached repository format")
    exports = {}
    for manifest in sorted((repo / "machines").glob("*/codex/manifest.json")):
        manifest = safe_path(repo, manifest.relative_to(repo).as_posix())
        meta = json.loads(inputs.read(manifest))
        if meta.get("version") != 1 or not isinstance(meta.get("files"), dict):
            raise SyncError("Invalid cached snapshot manifest")
        for rel, attrs in meta["files"].items():
            if not rel.startswith(codex.EXPORT_DIR + "/"):
                continue
            if not allowed("codex", rel):
                raise SyncError("Unsupported cached history path")
            data = inputs.read(safe_path(manifest.parent, "data/" + rel))
            if digest(data) != attrs.get("sha256"):
                raise SyncError("Cached history checksum mismatch")
            exports.setdefault(rel, []).append(data)
    return exports


def current_exports(root, rollouts, inputs):
    """Read DB/WAL as bytes; SQLite only opens a disposable copy (no source SHM writes)."""
    paginated = {rel: (data, 0) for rel, data in rollouts.items() if rollout(data)[1] == "paginated"}
    if not paginated:
        return {}
    with tempfile.TemporaryDirectory(prefix="agent-sync-audit-history-") as tmp:
        copy = Path(tmp)
        for suffix in ("", "-wal"):
            path = safe_path(root, codex.DB_NAME + suffix)
            data = inputs.read(path, optional=bool(suffix))
            if data is not None:
                (copy / path.name).write_bytes(data)
        inputs.verify()
        codex.export_history(copy, paginated)
    return {rel: [data] for rel, (data, _) in paginated.items() if rel.startswith(codex.EXPORT_DIR + "/")}


def build_report(state, cfg, tools):
    inputs = Inputs()
    inputs.read(state / "config.json")
    saved = conflicts(state, tools, inputs)
    result = {"report_version": 1, "read_only": True, "source": "cached snapshots and preserved alternatives",
              "conversations": [], "other_files": []}
    if not saved:
        result["summary"] = {"conflict_files": 0, "comparisons": 0, "conversations": 0, "classifications": {}}
        inputs.verify()
        return result
    exports = cached_exports(state, inputs) if any(t == "codex" for t, _, _, _ in saved) else {}
    local_files = {}
    for tool, rel, data, _ in saved:
        key = (tool, rel)
        if key not in local_files:
            local_files[key] = inputs.read(safe_path(Path(cfg[tool + "_dir"]), rel), optional=True)
        if tool == "codex" and rel.startswith(codex.EXPORT_DIR + "/"):
            exports.setdefault(rel, []).append(data)
    codex_rollouts = {}
    for (tool, rel), data in local_files.items():
        if tool == "codex" and transcript(tool, rel) and data is not None:
            try:
                rollout(data)
                codex_rollouts[rel] = data
            except SyncError:
                pass  # Report invalid inputs per comparison, never omit them.
    # Saved portable exports also survive pulls outside the cached snapshot.
    for tool, rel, data, _ in saved:
        if tool != "codex" or not transcript(tool, rel):
            continue
        try:
            tid, mode, _ = rollout(data)
            if mode == "paginated":
                export_rel = "{}/{}/{}.json".format(codex.EXPORT_DIR, tid, digest(data))
                stored = inputs.read(safe_path(Path(cfg["codex_dir"]), export_rel), optional=True)
                if stored is not None:
                    exports.setdefault(export_rel, []).append(stored)
        except SyncError:
            pass
    local_error = None
    try:
        local_exports = current_exports(Path(cfg["codex_dir"]), codex_rollouts, inputs)
    except (SyncError, OSError, sqlite3.Error) as exc:
        local_exports, local_error = {}, str(exc)
    grouped = {}
    for tool, rel, alternative, folder in saved:
        local = local_files[tool, rel]
        entry = {"tool": tool, "path": rel, "alternative_location": folder,
                 "local_sha256": digest(local) if local is not None else None,
                 "alternative_sha256": digest(alternative)}
        tid = None
        try:
            if tool == "codex" and transcript(tool, rel):
                tid = rollout(alternative)[0]
                if local is None:
                    raise SyncError("Local counterpart is missing")
                entry.update(compare_codex(rel, local, alternative, local_exports, exports))
                if local_error and rollout(local)[1] == "paginated":
                    entry.update(classification="inconclusive", reason=local_error)
            else:
                entry.update(classification="inconclusive", reason="Format-aware comparison supports Codex rollouts only",
                             bytes_equal=local == alternative)
        except (SyncError, ValueError, KeyError, TypeError) as exc:
            entry.update(classification="inconclusive", reason=str(exc))
        if tid:
            grouped.setdefault(tid, []).append(entry)
        else:
            result["other_files"].append(entry)
    for tid, comparisons in sorted(grouped.items()):
        classes = {c["classification"] for c in comparisons}
        overall = next(iter(classes)) if len(classes) == 1 else "inconclusive"
        result["conversations"].append({"thread_id": tid, "classification": overall, "comparisons": comparisons})
    counts = Counter(c["classification"] for c in result["conversations"])
    result["summary"] = {"conflict_files": len(local_files), "comparisons": len(saved),
                         "conversations": len(grouped), "classifications": dict(sorted(counts.items())),
                         "other_files": len(result["other_files"])}
    inputs.verify()
    result["inputs_verified"] = len(inputs.files)
    return result


def print_report(report, as_json=False):
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("Read-only conflict audit (cached data; no fetch or resolution).")
        print("{conflict_files} files; {comparisons} alternatives; {conversations} conversations.".format(**report["summary"]))
        for group in report["conversations"]:
            print("{}: {}".format(group["thread_id"], group["classification"]))
            for comparison in group["comparisons"]:
                print("  {}: {}".format(comparison["path"], comparison["classification"]))
                if "reason" in comparison:
                    print("    " + comparison["reason"])
                if comparison.get('known_content_classification') and comparison['classification'] == 'inconclusive':
                    print('    Verified portions: ' + comparison['known_content_classification'])
                unknown = comparison.get('sections', {}).get('unrecognized_records', {})
                for side in ('local', 'alternative'):
                    if unknown.get(side + '_types'):
                        print('    {} unrecognized types: {}'.format(side, canonical(unknown[side + '_types'])))
                for section, info in comparison.get("sections", {}).items():
                    print("    {}: {} (local {}, alternative {})".format(
                        section, info["relation"], info["local_count"], info["alternative_count"]))
                    if section == 'turn_context':
                        for field, detail in info.get('fields', {}).items():
                            if detail['relation'] != 'equal':
                                print('      {}: {} (local {}, alternative {})'.format(
                                    field, detail['relation'], detail['local_count'], detail['alternative_count']))
                print("    Preserved alternative: " + comparison["alternative_location"])
        for entry in report["other_files"]:
            print("{}: inconclusive — {}".format(entry["path"], entry["reason"]))
        print("Equivalence never authorizes deletion. All versions and recovery metadata remain untouched.")
    return 2 if (report["other_files"] or any(g["classification"] == "inconclusive"
                    for g in report["conversations"])) else 0
