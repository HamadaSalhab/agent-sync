# Security and data handling

Conversations and file-edit history can contain source code, secrets, private messages, and local paths. Use a private data repository. Optional git-crypt encrypts snapshot contents and manifests in Git; Git metadata, filenames, and sizes can remain visible. Previously published plaintext is not removed by later encryption.

agent-sync uses a strict file allowlist. It does not sync login credentials, `auth.json`, `.credentials.json`, SQLite databases, caches, settings, or browser state. This is not message-content redaction: a key pasted into a conversation remains in that conversation.

Snapshots are checked for path traversal, symlinks, allowed file types, and SHA-256 integrity before applying changes. Hashes detect corruption; they are not a signature proving who authored a snapshot. Only grant write access to people and machines you trust. Synced conversation contents are not executed by agent-sync.

Backups, conflict recovery files, and the unlocked local checkout are plaintext. The state directory and newly created files use owner-only permissions. Store encryption keys separately, and protect the machine with appropriate disk encryption and access controls.

Close the agents before syncing or restoring. Stable-read checks and atomic per-file replacement reduce partial writes, but they cannot provide a transaction across every file or stop a running agent from writing. Interrupted operations retain backups and may require a retry. The tool never force-pushes, resets a dirty checkout, or deletes local conversations.

Report security concerns privately to the maintainer of the fork you use. Do not include actual credentials, encryption keys, or private transcripts in public issues.
