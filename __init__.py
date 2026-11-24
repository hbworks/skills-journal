"""skills-journal plugin entry point.

``post_tool_call`` only marks dirty (cheap); the git work happens once per turn
in ``post_llm_call``, so a turn's edits land as one commit instead of one per
keystroke — and a runaway tool loop cannot produce a commit per call.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Set

try:  # loaded as a package (normal case)
    from . import journal
except (ImportError, ValueError):  # pragma: no cover - ad-hoc loading
    import journal  # type: ignore

logger = logging.getLogger(__name__)

_PATH_TOOLS = frozenset({"write_file", "patch"})
_ALWAYS_TOOLS = frozenset({"skill_manage"})

_lock = threading.Lock()
_dirty_tools: Set[str] = set()
_dirty = False
_reported_not_repo = False


def _under_root(raw: Any, root: Path) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(os.getcwd()) / path
        resolved = path.resolve()
        base = root.resolve()
    except (OSError, RuntimeError):
        return False
    if resolved == base / ".git" or (base / ".git") in resolved.parents:
        return False
    return resolved == base or base in resolved.parents


def touches_skills(tool_name: str, args: Optional[Dict[str, Any]], root: Path) -> bool:
    """Would this call have written anything under the skills directory?"""
    if tool_name in _ALWAYS_TOOLS:
        return True
    if tool_name in _PATH_TOOLS:
        return _under_root((args or {}).get("path"), root)
    if tool_name == "terminal":
        command = (args or {}).get("command") or ""
        if not isinstance(command, str):
            return False
        # Loose on purpose: a shell command that redirects into the skills dir
        # should still earn a commit attempt.
        return "skills/" in command and (root.name in command or str(root) in command)
    return False


def _mark(tool_name: str) -> None:
    global _dirty
    with _lock:
        _dirty = True
        _dirty_tools.add(tool_name or "unknown")


def _drain() -> tuple[bool, Set[str]]:
    global _dirty
    with _lock:
        was_dirty = _dirty
        tools = set(_dirty_tools)
        _dirty = False
        _dirty_tools.clear()
    return was_dirty, tools


def _flush(session_id: str = "", turn_id: str = "") -> Optional[str]:
    global _reported_not_repo
    was_dirty, tools = _drain()
    if not was_dirty:
        return None
    root = journal.skills_root()
    if not journal.is_repo(root):
        if not _reported_not_repo:
            _reported_not_repo = True
            logger.info(
                "skills-journal: %s is not a git repository yet — run `git init` there", root
            )
        return None
    journal.ensure_gitignore(root)
    sha = journal.commit(root, tools=tools, session_id=session_id, turn_id=turn_id)
    if sha:
        logger.debug("skills-journal: committed %s (%s)", sha, ", ".join(sorted(tools)) or "?")
    return sha


# --------------------------------------------------------------------------- hooks


def on_post_tool_call(
    tool_name: str = "", args: Optional[Dict[str, Any]] = None, **_: Any
) -> None:
    """Mark the turn dirty; never touch git here (parallel calls, cheap path)."""
    try:
        if not journal.enabled():
            return
        name = tool_name or ""
        if not name:
            return
        if name not in _ALWAYS_TOOLS and name not in _PATH_TOOLS and name != "terminal":
            return
        if touches_skills(name, args, journal.skills_root()):
            _mark(name)
    except Exception:  # pragma: no cover - must never break the tool loop
        logger.debug("skills-journal: post_tool_call failed", exc_info=True)


def on_post_llm_call(
    session_id: str = "", turn_id: str = "", **_: Any
) -> None:
    """End of turn: commit this turn's skill edits as one entry."""
    try:
        _flush(session_id=session_id, turn_id=turn_id)
    except Exception:  # pragma: no cover
        logger.debug("skills-journal: post_llm_call failed", exc_info=True)


def on_session_end(
    session_id: str = "", completed: bool = True, interrupted: bool = False, **_: Any
) -> None:
    """Backstop for edits made outside a normal turn (subagents, interrupt)."""
    try:
        _flush(session_id=session_id, turn_id="")
    except Exception:  # pragma: no cover
        logger.debug("skills-journal: on_session_end failed", exc_info=True)


def register(ctx) -> None:
    getter = getattr(ctx, "get_config", None)
    if callable(getter):
        journal.configure(getter)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
    logger.info("skills-journal registered (enabled=%s)", journal.enabled())


__all__ = ["register"]
