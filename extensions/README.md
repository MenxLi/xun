# Extensions

Drop Python code under `$XUN_HOME/extensions/` (default `./.xun/extensions/`, override with `XUN_HOME`) and every agent picks up its effects — tools, hooks, commands, config tweaks — at initialization. No registration or wiring needed.

Extensions are **trusted code**: they run at import time with full process privileges, like a shell rc file.

To see if your extensions were loaded, use built-in `/extensions` command.

## Source forms

Each source yields one extension named `{name}`:

| Form | Path | Notes |
|---|---|---|
| Package | `extensions/{name}/setup_extension.py` | Entry file name is fixed; supports relative imports of sibling helper modules |
| Flat | `extensions/{name}.py` | Zero ceremony; no relative imports |

A directory without `setup_extension.py` is skipped with a warning; if both forms exist for the same `{name}`, the flat file is shadowed by the package. Files starting with `.` or `__` are ignored.

## Entry function

The module must define `setup_extension(ctx)`, called once per agent with the agent being initialized:

```python
"""Log every tool call."""
from xun import ExtensionContext

def setup_extension(ctx: ExtensionContext) -> None:
    ctx.agent.hooks.before_tool_call.add(lambda args: print(args.tool_calls))
```

`ctx` carries the target `agent` plus the extension's `name`. Typical effects, all through `ctx.agent`:

- **Hooks**: `ctx.agent.hooks.before_tool_call.add(...)` (see `src/xun/hooks.py`)
- **Tools**: define functions and register them on `ctx.agent.toolbox`, e.g. via `@tool_attr(name="web_search", override=True)` to replace a built-in tool
- **Config**: mutate `ctx.agent.config` (setup runs before model auto-detect, so overrides take effect)

The first line of the module docstring becomes the extension's description, shown by the `/extensions` command.

## Version compatibility

Declare the xun versions an extension supports on the entry function; both bounds are inclusive:

```python
"""Needs a recent xun."""
from xun import ExtensionContext, extension_attr

@extension_attr(api_min_version="1.2", api_max_version="2.0")
def setup_extension(ctx: ExtensionContext) -> None:
    ...
```

Outside the range the extension is **skipped** — listed as `skipped` by `/extensions` with the reason, warned once at scan time, never imported into agents, and never counted as `failed`. Query the running version with `xun_version()`; when xun runs without package metadata (a source checkout) the gate passes.

## Load semantics

- Sources are imported **once per process** (cached), then `setup_extension` is re-run with a fresh context for every agent — including sub-agents.
- Extensions apply in **name-sorted order**, deterministic across both forms.
- Import failure or setup failure only prints a warning: a broken extension never blocks startup or the other extensions (this README itself is skipped as a non-source file since it isn't `.py`). A version mismatch is likewise a warning, and shows up as `skipped` rather than `failed`.
- Opt out with `enable_extensions: false` in `$XUN_HOME/config.json`; internal helper agents set this automatically.

The implementation lives in [`src/xun/extension.py`](../src/xun/extension.py). Any `.py` source alongside this README serves as a working example.
