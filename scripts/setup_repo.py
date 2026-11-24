#!/usr/bin/env python3
"""One-shot setup: make the profile's skills directory a git repository.

    python3 scripts/setup_repo.py [--root ~/.hermes/profiles/<name>/skills] [--force]

* ``git init`` when the directory is not a repository yet (never clobbers one).
* (Re)write ``.gitignore`` from ``.bundled_manifest``; ``--force`` ignores the
  mtime gate so the ignore list is rebuilt after manual edits.
* Make the first commit if anything is trackable.

Idempotent: running it twice changes nothing the second time.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from journal import (  # noqa: E402
    commit,
    ensure_gitignore,
    is_repo,
    pristine_bundled_dirs,
    render_gitignore,
    skills_root,
)


def _git(root: Path, *args: str) -> int:
    import subprocess

    return subprocess.run(["git", "-C", str(root), *args]).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None, help="skills directory (default: this profile's)")
    parser.add_argument("--force", action="store_true", help="rebuild .gitignore regardless of mtime")
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve() if args.root else skills_root()
    if not root.is_dir():
        print(f"no such skills directory: {root}", file=sys.stderr)
        return 2

    if not is_repo(root):
        code = _git(root, "init", "-q", "-b", "main")
        if code != 0:
            print("git init failed", file=sys.stderr)
            return 1
        print(f"initialized git repository: {root}")
    else:
        print(f"already a git repository: {root}")

    if args.force:
        (root / ".gitignore").write_text(render_gitignore(root), encoding="utf-8")
        changed = True
    else:
        changed = ensure_gitignore(root)
    print(f".gitignore {'updated' if changed else 'unchanged'}")

    pristine = pristine_bundled_dirs(root)
    print(f"pristine bundled skills ignored: {len(pristine)}")
    for path in pristine[:10]:
        print(f"  - {path}/")
    if len(pristine) > 10:
        print(f"  ... and {len(pristine) - 10} more")

    sha = commit(root, extra_note="setup")
    print(f"commit: {sha}" if sha else "nothing to commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
