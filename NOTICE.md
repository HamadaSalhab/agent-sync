# Origin and attribution

agent-sync is a fork and evolution of **Claude Code Conversation Sync**, originally authored by **porkchop** and its contributors:

- Original project: https://github.com/porkchop/claude-code-sync
- Modified fork used as the starting point: https://github.com/HamadaSalhab/claude-code-sync
- Starting commit in that fork: `7bf91a8` — `fix: preserve conversation mtimes across machines`
- Original license: MIT; original copyright: `Copyright (c) 2024 Claude Code Sync Contributors`

The initial agent-sync repository retains the source project's Git history. The new Python implementation generalizes its Git-based sync, local backup, encryption, and timestamp-preservation approach to multiple tools. It replaces the original Bash commands and adds independent machine snapshots, explicit conflict recovery, and Codex session support.

The original MIT copyright and permission notice remain in `LICENSE`. Please preserve this attribution when redistributing this project or substantial portions of it.
