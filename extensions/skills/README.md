# Skills Extension

Optional support for [Agent Skills](https://agentskills.io) `SKILL.md` bundles.
It is an extension rather than core xun functionality: skills are portable
instructions, while xun's core only provides generic persistent system-prompt
sections and command execution.

## Install

Copy this directory to `$XUN_HOME/extensions/skills/`, then install its only
dependency in the environment that runs xun:

```bash
pip install pyyaml
```

The extension discovers bundles from these roots, in precedence order:

1. `$XUN_HOME/skills/`
2. `<workdir>/.agents/skills/`
3. `~/.agents/skills/`

The first bundle with a given name wins. Each bundle needs a `SKILL.md` with YAML
frontmatter containing a `name` and `description`.

## Design

`catalog.py` owns discovery, frontmatter parsing, session state, and the compact
catalog written to the generic `persist_sections` prompt field. The model sees
only names and descriptions until it calls `activate_skill`.

`tools.py` owns the model-facing tools. Reading bundled files and running scripts
requires activation. Script execution is argv-based rather than shell-based and
requires an explicit confirmation; a user may grant a specific script for the
current session.

`setup_extension.py` is intentionally small: it always registers the `/skills`
command, while the model-facing tools follow the catalog through `sync_tools` and
only appear once at least one skill has been discovered. A bundle is usually created
after the agent has started, so `/skills reload` re-runs `sync_tools` and the new
bundle gains its tools without a restart. Missing PyYAML makes this extension fail
to import, while the core agent continues to start normally.

`/skills` lists discovered skills, printing the roots it searched whenever the
catalog is empty. Use `/skills info <name>` to inspect metadata without changing the
session, `/skills activate <name>` to load instructions, `/skills reload` to rescan.

## Test

Run the extension suite independently because it relies on the optional PyYAML
dependency:

```bash
uv run python -m unittest extensions.skills.test_skills
```