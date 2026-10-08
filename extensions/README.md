# Extensions

Drop Python code under `$XUN_HOME/extensions/` (default `./.xun/extensions/`) and every agent picks up its effects — tools, hooks, commands, config tweaks — at initialization. No registration needed. Extensions are **trusted code**: they run at import time with full process privileges, like a shell rc file. Check what loaded with the built-in `/extensions` command.

## Source forms

Each source yields one extension named `{name}`:

| Form | Path | Notes |
|---|---|---|
| Package | `extensions/{name}/setup_extension.py` | supports relative imports of siblings |
| Flat | `extensions/{name}.py` | zero ceremony; no relative imports |

A directory without `setup_extension.py` is skipped with a warning; the package form shadows the flat file of the same name; `.`/`__` files are ignored. The first line of the module docstring becomes the description shown by `/extensions`.

## Entry function

The module defines `setup_extension(ctx)`, called once per agent — including sub-agents — with a fresh context:

```python
"""Log every tool call."""
from xun import ExtensionContext

def setup_extension(ctx: ExtensionContext) -> None:
    ctx.agent.hooks.before_tool_call.add(lambda args: print(args.tool_calls))
```

Typical effects, all through `ctx.agent`:

- **Hooks**: `ctx.agent.hooks.<event>.add(...)` (see `src/xun/hooks.py`)
- **Tools**: register functions on `ctx.agent.toolbox` (`@tool_attr(override=True)` replaces a built-in)
- **Config**: mutate `ctx.agent.config` (setup runs before model auto-detect)
- **Settings**: `ctx.settings` — the extension's bag from `config.json`, keyed by name: `{ "extension_settings": { "my_ext": { ... } } }`
- **Files**: `ctx.data_dir()` — private dir `$XUN_HOME/extension_data/{name}/`, created on demand

## Declaring metadata

`@extension_attr` on the entry function declares three optional things:

```python
import argparse
from xun import ExtensionContext, extension_attr

_parser = argparse.ArgumentParser()
_parser.add_argument("--user", default="me")

def _run(args):
    print(args.user)
    return 0

@extension_attr(api_min_version="1.2", api_max_version="2.0",
                data_version="1", cli=(_parser, _run))
def setup_extension(ctx: ExtensionContext) -> None:
    ...
```

- **`api_min_version` / `api_max_version`** — supported xun versions, both bounds inclusive. Outside the range the extension is **skipped** (not `failed`): warned at scan, listed with the reason by `/extensions`. Source checkouts without version metadata always pass the gate.
- **`data_version`** — version of the data layout: `data_dir()` resolves to `extension_data/{name}/data_v_<version>/`, so bumping it starts fresh while old dirs stay untouched. One safe path segment; a bad value fails the extension at scan.
- **`cli=(parser, handler)`** — run by the core `xune` command: `xune <name> [args...]` parses with the extension's parser and calls the handler (an int return is the exit code); bare `xune` lists extensions that declare a CLI. The core imports the extension, so its data under `$XUN_HOME` resolves exactly as the agent sees it — no `PYTHONPATH` or module paths to fiddle with.

See `stat/` for a full example.

## Load semantics

- Sources are imported **once per process** (cached); `setup_extension` re-runs per agent.
- Extensions apply in **name-sorted order**.
- Import or setup failure only warns: a broken extension never blocks startup or the others.
- Opt out with `enable_extensions: false` in `$XUN_HOME/config.json` (internal helper agents set this automatically).

`skills/` is an optional bundled extension for `SKILL.md` bundles — copy it in and `pip install pyyaml`. Implementation: [`src/xun/extension.py`](../src/xun/extension.py); any `.py` alongside this README is a working example.
