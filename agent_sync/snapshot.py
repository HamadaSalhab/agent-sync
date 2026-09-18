"""Lossless, checksummed storage for files larger than Git hosting limits."""

import re

from .files import SyncError, atomic_write, digest, safe_path

CHUNK_SIZE = 32 * 1024 * 1024
VERSIONS = (1, 2)


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def write_file(root, rel, data, stamp):
    target = safe_path(root, "data/" + rel)
    attrs = {"sha256": digest(data), "mtime_ns": stamp}
    if len(data) <= CHUNK_SIZE:
        if not target.exists() or target.read_bytes() != data:
            atomic_write(target, data)
        return attrs
    attrs.update(size=len(data), chunks=[])
    for start in range(0, len(data), CHUNK_SIZE):
        part = data[start:start + CHUNK_SIZE]
        sha = digest(part)
        path = safe_path(root, "chunks/" + sha)
        if not path.exists() or path.read_bytes() != part:
            atomic_write(path, part)
        attrs["chunks"].append({"sha256": sha, "size": len(part)})
    # The original is fully retained in the source and backups; only its Git
    # transport representation changes. Leaving it tracked would defeat chunks.
    if target.exists():
        target.unlink()
    return attrs


def read_file(root, rel, attrs, version, read=None):
    read = read or (lambda path: path.read_bytes())
    try:
        if type(version) is not int or version not in VERSIONS or not isinstance(attrs, dict):
            raise ValueError("unsupported snapshot metadata")
        stamp = attrs["mtime_ns"]
        if type(stamp) is not int or not 0 <= stamp <= 9223372036854775807 or not valid_sha(attrs["sha256"]):
            raise ValueError("invalid checksum or modification time")
        if "chunks" not in attrs:
            data = read(safe_path(root, "data/" + rel))
        else:
            if version != 2 or type(attrs.get("size")) is not int or attrs["size"] <= 0:
                raise ValueError("invalid chunked file size/version")
            chunks = attrs["chunks"]
            if not isinstance(chunks, list) or not chunks:
                raise ValueError("missing chunks")
            parts, total = [], 0
            for chunk in chunks:
                if (not isinstance(chunk, dict) or set(chunk) != {"sha256", "size"}
                        or not valid_sha(chunk["sha256"]) or type(chunk["size"]) is not int
                        or not 0 < chunk["size"] <= CHUNK_SIZE):
                    raise ValueError("invalid chunk descriptor")
                total += chunk["size"]
                if total > attrs["size"]:
                    raise ValueError("chunks exceed original size")
                part = read(safe_path(root, "chunks/" + chunk["sha256"]))
                if len(part) != chunk["size"] or digest(part) != chunk["sha256"]:
                    raise ValueError("chunk checksum/size mismatch")
                parts.append(part)
            if total != attrs["size"]:
                raise ValueError("incomplete chunked file")
            data = b"".join(parts)
        if digest(data) != attrs["sha256"]:
            raise ValueError("file checksum mismatch")
        return data, stamp
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SyncError("Invalid snapshot file {}: {}".format(rel, exc))
