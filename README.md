# skills-journal

Keeps a Hermes profile's `skills/` directory in git, so an edited skill — or a script shipped
with it — can be reverted with `git checkout <commit>~1 -- <path>` instead of replaying `patch`
hunks from the session history.

## What it does

| Hook | Action |
|---|---|
| `post_tool_call` | Marks the turn dirty when the call wrote into `$HERMES_HOME/skills` (`skill_manage`, `write_file`, `patch`, or a `terminal` command naming that path). No git work, no I/O. |
| `post_llm_call` | At the response boundary, commits the accumulated changes as **one** entry. |
| `on_session_end` | Backstop for edits made outside a normal turn (subagent, interrupt). |

Commit message:

```
skills: research/arxiv, autonomous-ai-agents/hermes-agent

tools: patch, write_file
session 1a2b3c4d / turn 9f8e7d6c
files:
  - research/arxiv/SKILL.md
```

## Setup (once per profile)

```sh
python3 scripts/setup_repo.py            # this profile's skills dir
python3 scripts/setup_repo.py --root ~/.hermes/profiles/<name>/skills --force
```

`setup_repo.py` runs `git init` if needed, writes `.gitignore`, and makes the first commit. It is
idempotent. Then symlink this directory into `<profile>/plugins/` and enable it:

```sh
ln -sfn "$PWD" ~/.hermes/profiles/<name>/plugins/skills-journal
hermes plugins enable skills-journal      # the gateway reloads hooks in place
```

## Scope rules

* **Pristine bundled skills are ignored.** Each manifest entry is re-hashed with the host's own
  `tools.skills_sync.dir_hash`, so a skill untouched since `hermes update` stays out of the
  history; it is upstream's text and `hermes skills reset <name> --restore` restores it.
* **A bundled skill you edited stays tracked** — that is exactly the case where a full reset is
  too coarse and per-file rollback is what you want.
* **Everything you authored is tracked.**
* Generated state is ignored: `.locks/`, `*.lock`, `.usage.json*`, `.curator_ledger.jsonl`,
  `.curator_state`, `.curator_backups/`, `.skills_prompt_snapshot.json`, `.hub/`, `.archive/`,
  `__pycache__/`.

The `.gitignore` is regenerated only when `.bundled_manifest` is newer than it (hashing every
bundled skill reads the whole tree). `--force` rebuilds it after a manual edit.

## Settings

```sh
hermes config set plugins.entries.skills-journal.settings.enabled true
hermes config set plugins.entries.skills-journal.settings.ignore_pristine_bundled true
hermes config set plugins.entries.skills-journal.settings.author_name "…"
hermes config set plugins.entries.skills-journal.settings.author_email "…"
```

Environment overrides: `SKILLS_JOURNAL_ENABLED`, `SKILLS_JOURNAL_IGNORE_PRISTINE_BUNDLED`,
`SKILLS_JOURNAL_AUTHOR_NAME`, `SKILLS_JOURNAL_AUTHOR_EMAIL`.

## Reverting

```sh
git -C ~/.hermes/profiles/<name>/skills log --oneline -- research/arxiv
git -C ~/.hermes/profiles/<name>/skills checkout <commit>~1 -- research/arxiv/SKILL.md
```

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Any Python 3.10+ works. When the system `python3` is older, use the interpreter Hermes ships:
`~/.hermes/tools/python-*/bin/python3`.

Safe by design: every hook entry point catches and logs, git calls carry a 10s timeout, and
commits are serialized with a lock (`post_tool_call` fires concurrently). With the directory not
a git repository the plugin is a silent no-op — it logs that fact once.
