from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil

IGNORED = {".git", ".svn", ".hg", ".router-state", ".router-dev", "__pycache__", ".venv", "node_modules", ".env"}


def is_link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("Use a nonempty relative POSIX path")
    rel = PurePosixPath(relative)
    if rel.is_absolute() or any(p in {"..", ".git", ".svn", ".hg", ".env"} or p.startswith(".env.") for p in rel.parts):
        raise ValueError("Path escapes scope or targets VCS metadata")
    root = root.resolve()
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if is_link(cursor):
            raise ValueError("Symlink/junction paths are not permitted")
    result = cursor.resolve()
    if result == root or root not in result.parents:
        raise ValueError("Path must be a file within workspace")
    return result


def forbidden_artifacts(root):
    """Detect newly created metadata/secrets even though they are excluded from snapshots."""
    found = []
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            if name in {".git", ".svn", ".hg", ".env"} or name.startswith(".env."):
                found.append((Path(current) / name).relative_to(root).as_posix())
        dirs[:] = [d for d in dirs if d not in IGNORED]
    return found


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def permitted(path, allowed, forbidden):
    return bool(allowed) and matches(path, allowed) and not matches(path, forbidden)


def inventory(root: Path, max_bytes=100_000_000, max_files=10000):
    result, total = {}, 0
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED)
        for name in dirs + files:
            p = Path(current) / name
            if name in IGNORED:
                continue
            if is_link(p):
                raise ValueError(f"Cannot safely snapshot symlink/junction: {p}")
        for name in sorted(files):
            if name in IGNORED or name.startswith(".env."):
                continue
            path = Path(current) / name
            total += path.stat().st_size
            if total > max_bytes or len(result) >= max_files:
                raise ValueError("Workspace snapshot limit exceeded (100MB / 10000 files)")
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def copy_snapshot(source, target):
    files = inventory(source)
    target.mkdir(parents=True, exist_ok=False)
    for relative in files:
        dest = safe_path(target, relative)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(safe_path(source, relative), dest)
    if inventory(target) != files:
        raise RuntimeError("Source changed during snapshot; retry with a stable workspace")
    return files


class Workspace:
    def __init__(self, source: Path, run_dir: Path):
        self.source, self.run_dir = source.resolve(), run_dir.resolve()
        if self.source == self.run_dir or self.source in self.run_dir.parents:
            raise ValueError("State/run directory must be outside the target repository")
        self.vcs = next((kind for marker, kind in ((".git", "git"), (".svn", "svn"), (".hg", "hg"))
                         if any((p / marker).exists() for p in [self.source, *self.source.parents])), "none")
        self.base, self.work = self.run_dir / "baseline", self.run_dir / "work"

    def prepare(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        original = copy_snapshot(self.source, self.base)
        copy_snapshot(self.base, self.work)
        (self.run_dir / "workspace.json").write_text(json.dumps({
            "source": str(self.source), "vcs": self.vcs, "original_hashes": original,
            "isolation": "copy", "excluded_names": sorted(IGNORED)}, indent=2), encoding="utf-8")

    def checkpoint(self, name):
        copy_snapshot(self.work, self.run_dir / name)

    def diff(self):
        before, after = inventory(self.base), inventory(self.work)
        changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
        parts = []
        for relative in changed:
            a = safe_path(self.base, relative)
            b = safe_path(self.work, relative)
            try:
                old = a.read_text(encoding="utf-8").splitlines(keepends=True) if a.exists() else []
                new = b.read_text(encoding="utf-8").splitlines(keepends=True) if b.exists() else []
                parts.extend(difflib.unified_diff(old, new, fromfile="a/" + relative, tofile="b/" + relative))
            except UnicodeError:
                parts.append(f"Binary files differ: {relative}\n")
        (self.run_dir / "changes.diff").write_text("".join(parts), encoding="utf-8")
        return changed


def promote(run_dir: Path):
    """Explicit user command only. Originals unchanged until all preconditions pass."""
    run_dir = run_dir.resolve()
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    if result["status"] != "verified":
        raise ValueError("Only independently verified runs can be promoted")
    meta = json.loads((run_dir / "workspace.json").read_text(encoding="utf-8"))
    source, work = Path(meta["source"]).resolve(), run_dir / "work"
    if inventory(source) != meta["original_hashes"]:
        raise ValueError("Original workspace changed since snapshot; refusing to overwrite")
    verified = json.loads((run_dir / "verified-hashes.json").read_text(encoding="utf-8"))
    if inventory(work) != verified:
        raise ValueError("Candidate changed after verification")
    changed = [p for p in meta["original_hashes"].keys() | verified.keys()
               if meta["original_hashes"].get(p) != verified.get(p)]
    # Check every path before the first mutation. The immutable baseline is the recovery point.
    paths = [(p, safe_path(source, p), safe_path(work, p)) for p in changed]
    applied = []
    try:
        for relative, dest, candidate in paths:
            applied.append(relative)
            if candidate.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, dest)
            elif dest.exists():
                dest.unlink()
    except BaseException:
        for relative in reversed(applied):
            dest, backup = safe_path(source, relative), safe_path(run_dir / "baseline", relative)
            if backup.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, dest)
            elif dest.exists():
                dest.unlink()
        raise
    (run_dir / "promoted.json").write_text(json.dumps({"files": changed}), encoding="utf-8")
    return changed
