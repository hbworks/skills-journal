"""skills-journal core logic.

Goal: an edited skill (and any script shipped with it) can be reverted with
``git checkout <commit>~1 -- <path>`` instead of replaying ``patch`` hunks from
the session history.

Scope rules
-----------
* The profile's ``skills/`` directory is the git repository (``git init`` there).
* A bundled skill whose content still matches ``.bundled_manifest`` is
  gitignored — it is upstream's text and ``hermes skills reset <name>
  --restore`` already restores it.
* A bundled skill you edited stays **tracked**, because that is exactly the case
  where a full reset is too coarse and a per-file revert is what you want.
* Everything you authored is tracked.

The plugin never raises into the agent loop: every entry point catches and logs.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 10
MAX_BODY_FILES = 30
MAX_SUBJECT_SKILLS = 3

IGNORE_HEADER = """\
# Managed by skills-journal (regenerated from .bundled_manifest) -- do not hand-edit.
# Pristine bundled skills are ignored here; restore those with:
#   hermes skills reset <name> --restore
# Skills you authored or edited are tracked, so `git log` / `git checkout` cover them.
"""

# Generated/runtime state. Never worth a commit; churns every session.
VOLATILE_PATTERNS: tuple[str, ...] = (
    ".DS_Store",
    "__pycache__/",
    "*.pyc",
    ".locks/",
    "*.lock",
    ".usage.json",
    ".usage.json.lock",
    ".curator_ledger.jsonl",
    ".curator_state",
    ".curator_backups/",
    ".skills_prompt_snapshot.json",
    ".hub/",
    ".archive/",
)

# Directories that never count as skill content when hashing.
_CACHE_DIRS = frozenset({"__pycache__", ".locks", ".hub", ".curator_backups"})

_COMMIT_LOCK = threading.Lock()
_CONFIG: Optional[Callable[..., Any]] = None

_ENV_KEYS: Dict[str, str] = {
    "enabled": "SKILLS_JOURNAL_ENABLED",
    "ignore_pristine_bundled": "SKILLS_JOURNAL_IGNORE_PRISTINE_BUNDLED",
    "author_name": "SKILLS_JOURNAL_AUTHOR_NAME",
    "author_email": "SKILLS_JOURNAL_AUTHOR_EMAIL",
}


# --------------------------------------------------------------------------- config


def configure(get_config: Optional[Callable[..., Any]] = None) -> None:
    """Wire the plugin to Hermes' plugin-config reader (``ctx.get_config``)."""
    global _CONFIG
    _CONFIG = get_config


def _raw_setting(key: str) -> Any:
    env_name = _ENV_KEYS.get(key)
    if env_name and os.environ.get(env_name) is not None:
        return os.environ.get(env_name)
    if _CONFIG is not None:
        try:
            return _CONFIG(key, None)
        except Exception:  # pragma: no cover - config reader is best-effort
            return None
    return None


def _flag(key: str, default: bool) -> bool:
    raw = _raw_setting(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    return _flag("enabled", True)


def ignore_pristine_bundled() -> bool:
    return _flag("ignore_pristine_bundled", True)


def _author() -> tuple[Optional[str], Optional[str]]:
    name = _raw_setting("author_name")
    mail = _raw_setting("author_email")
    return (str(name) if name else None, str(mail) if mail else None)


# --------------------------------------------------------------------------- paths


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home()).expanduser()
    except Exception:
        raw = os.environ.get("HERMES_HOME", "").strip()
        return Path(raw).expanduser() if raw else Path.home() / ".hermes"


def skills_root() -> Path:
    return hermes_home() / "skills"


def is_repo(root: Path) -> bool:
    git_dir = root / ".git"
    return git_dir.is_dir() or git_dir.is_file()


# --------------------------------------------------------------------------- git


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    name, mail = _author()
    cmd: List[str] = ["git", "-C", str(root)]
    if name:
        cmd += ["-c", f"user.name={name}"]
    if mail:
        cmd += ["-c", f"user.email={mail}"]
    cmd += list(args)
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT, env=env
    )


def _status_entries(root: Path) -> List[str]:
    """Changed paths (relative, posix), from ``git status --porcelain``."""
    res = _git(root, "status", "--porcelain", "--untracked-files=all")
    if res.returncode != 0:
        logger.debug("skills-journal: git status failed: %s", (res.stderr or "").strip())
        return []
    out: List[str] = []
    for line in res.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:  # rename: keep the destination
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            out.append(path)
    return out


# --------------------------------------------------------------------------- hashing


def _fallback_dir_hash(directory: Path) -> str:
    hasher = hashlib.md5()
    try:
        files = sorted(directory.rglob("*"))
    except OSError:
        return ""
    for path in files:
        try:
            if not path.is_file():
                continue
            rel = path.relative_to(directory)
        except OSError:
            continue
        if any(part in _CACHE_DIRS for part in rel.parts) or path.suffix == ".pyc":
            continue
        hasher.update(str(rel).encode("utf-8"))
        try:
            hasher.update(path.read_bytes())
        except OSError:
            return ""
    return hasher.hexdigest()


def dir_hash(directory: Path) -> str:
    """Same digest ``hermes skills`` uses to prove a bundled copy is pristine."""
    try:
        from tools.skills_sync import dir_hash as upstream_dir_hash  # type: ignore

        return upstream_dir_hash(directory)
    except Exception:
        return _fallback_dir_hash(directory)


def read_manifest(root: Path) -> Dict[str, str]:
    """``{skill_name: origin_hash}`` from ``.bundled_manifest`` (v1 lines have no hash)."""
    entries: Dict[str, str] = {}
    try:
        text = (root / ".bundled_manifest").read_text(encoding="utf-8-sig")
    except OSError:
        return entries
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, digest = line.partition(":")
        if name.strip():
            entries[name.strip()] = digest.strip()
    return entries


def _skill_dirs(root: Path) -> Iterable[Path]:
    try:
        candidates = sorted(root.rglob("SKILL.md"))
    except OSError:
        return []
    result = []
    for marker in candidates:
        directory = marker.parent
        try:
            rel = directory.relative_to(root)
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        result.append(directory)
    return result


def pristine_bundled_dirs(root: Path) -> List[str]:
    """Relative dir paths (``category/skill``) of bundled skills still matching the manifest."""
    manifest = read_manifest(root)
    if not manifest:
        return []
    pristine: List[str] = []
    for directory in _skill_dirs(root):
        origin = manifest.get(directory.name)
        if not origin:
            continue
        if dir_hash(directory) == origin:
            pristine.append(directory.relative_to(root).as_posix())
    return sorted(pristine)


# --------------------------------------------------------------------------- .gitignore


def render_gitignore(root: Path) -> str:
    lines = [IGNORE_HEADER.rstrip("\n"), ""]
    lines.extend(VOLATILE_PATTERNS)
    if ignore_pristine_bundled():
        pristine = pristine_bundled_dirs(root)
        if pristine:
            lines.append("")
            lines.append("# Bundled skills whose content matches .bundled_manifest.")
            lines.extend(f"/{path}/" for path in pristine)
    lines.append("")
    return "\n".join(lines)


def ensure_gitignore(root: Path) -> bool:
    """(Re)write ``.gitignore`` when the manifest moved on; True when it changed.

    Recomputed only when ``.bundled_manifest`` is newer than ``.gitignore``,
    because hashing every bundled skill costs a full read of the tree.
    """
    target = root / ".gitignore"
    manifest = root / ".bundled_manifest"
    try:
        if target.exists() and manifest.exists():
            if target.stat().st_mtime >= manifest.stat().st_mtime:
                return False
        elif target.exists():
            return False
        content = render_gitignore(root)
        if target.exists() and target.read_text(encoding="utf-8") == content:
            return False
        target.write_text(content, encoding="utf-8")
        return True
    except OSError as exc:
        logger.debug("skills-journal: gitignore update failed: %s", exc)
        return False


# --------------------------------------------------------------------------- commit


def skill_name_for(rel_path: str, root: Path) -> str:
    """Nearest ancestor directory holding a SKILL.md, else the top path component.

    A repo-root file (``.gitignore``, ``.bundled_manifest``) belongs to no skill
    and returns an empty string, so it never pollutes the commit subject.
    """
    parts = Path(rel_path).parts
    for depth in range(len(parts) - 1, 0, -1):
        candidate = root.joinpath(*parts[:depth])
        if (candidate / "SKILL.md").exists():
            return Path(*parts[:depth]).as_posix()
    if len(parts) > 1:
        return parts[0]
    return ""


def _subject(names: Sequence[str], total: int) -> str:
    shown = list(names[:MAX_SUBJECT_SKILLS])
    subject = "skills: " + ", ".join(shown)
    extra = max(total - len(shown), 0)
    if extra:
        subject += f" (+{extra})"
    return subject[:100]


def pending_changes(root: Path) -> List[str]:
    if not is_repo(root):
        return []
    return _status_entries(root)


def commit(
    root: Path,
    *,
    tools: Optional[Iterable[str]] = None,
    session_id: str = "",
    turn_id: str = "",
    extra_note: str = "",
) -> Optional[str]:
    """Commit whatever is dirty under ``root``. Returns the new sha, else None."""
    if not is_repo(root):
        return None
    with _COMMIT_LOCK:
        changed = _status_entries(root)
        if not changed:
            return None
        added = _git(root, "add", "-A")
        if added.returncode != 0:
            logger.debug("skills-journal: git add failed: %s", (added.stderr or "").strip())
            return None
        staged = _git(root, "diff", "--cached", "--quiet")
        if staged.returncode == 0:  # nothing actually staged
            return None

        names = sorted({name for name in (skill_name_for(path, root) for path in changed) if name})
        subject = _subject(names, len(names)) if names else "skills: repo files"
        body = []
        tool_list = sorted({t for t in (tools or []) if t})
        if tool_list:
            body.append("tools: " + ", ".join(tool_list))
        refs = []
        if session_id:
            refs.append(f"session {session_id[:8]}")
        if turn_id:
            refs.append(f"turn {turn_id[:8]}")
        if extra_note:
            refs.append(extra_note)
        if refs:
            body.append(" / ".join(refs))
        if changed:
            body.append("files:")
            body.extend(f"  - {path}" for path in changed[:MAX_BODY_FILES])
            if len(changed) > MAX_BODY_FILES:
                body.append(f"  ... and {len(changed) - MAX_BODY_FILES} more")

        res = _git(root, "commit", "--no-gpg-sign", "-m", subject, "-m", "\n".join(body))
        if res.returncode != 0:
            logger.debug("skills-journal: git commit failed: %s", (res.stderr or "").strip())
            return None
        sha = _git(root, "rev-parse", "--short", "HEAD")
        return (sha.stdout or "").strip() or "committed"
