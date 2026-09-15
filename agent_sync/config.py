import json
import os
import uuid
from pathlib import Path

from .files import SyncError, atomic_write, encode

TOOLS = ("claude", "codex")


def absolute(value):
    return Path(value).expanduser().resolve()


def state_path(value=None):
    return absolute(value or os.environ.get("AGENT_SYNC_HOME", "~/.local/share/agent-sync"))


def load(state):
    path = state / "config.json"
    if not path.is_file():
        raise SyncError("Not initialized. Run agent-sync init --remote <git-url> first.")
    try:
        cfg = json.loads(path.read_text())
        if not isinstance(cfg, dict) or cfg["version"] != 1 or not cfg["tools"] or not set(cfg["tools"]) <= set(TOOLS):
            raise ValueError("unsupported configuration")
        uuid.UUID(cfg["machine_id"])
        for tool in TOOLS:
            cfg[tool + "_dir"] = str(absolute(os.environ.get(
                "CLAUDE_DATA_DIR" if tool == "claude" else "CODEX_HOME", cfg[tool + "_dir"])))
        check_roots(state, cfg)
        return cfg
    except (ValueError, KeyError, TypeError) as exc:
        raise SyncError("Invalid config {}: {}".format(path, exc))


def check_roots(state, cfg):
    roots = [state.resolve()] + [absolute(cfg[t + "_dir"]) for t in TOOLS]
    for i, a in enumerate(roots):
        for b in roots[i + 1:]:
            if a == b or a in b.parents or b in a.parents:
                raise SyncError("State and agent directories must not overlap: {} and {}".format(a, b))


def create(state, remote, branch, tools, claude_dir=None, codex_dir=None):
    cfg = {"version": 1, "machine_id": str(uuid.uuid4()), "remote": remote,
           "branch": branch, "tools": tools,
           "claude_dir": str(absolute(claude_dir or os.environ.get("CLAUDE_DATA_DIR", "~/.claude"))),
           "codex_dir": str(absolute(codex_dir or os.environ.get("CODEX_HOME", "~/.codex")))}
    check_roots(state, cfg)
    return cfg


def save(state, cfg):
    atomic_write(state / "config.json", encode(cfg))
