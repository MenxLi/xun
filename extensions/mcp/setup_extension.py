"""Expose the stdio MCP servers listed in extension settings as xun tools.

Configure under `extension_settings.mcp` in `config.json`:

    {"extension_settings": {"mcp": {"servers": {
        "toy": {"command": "python3", "args": ["toy_server.py"],
                "env": {"TOY_KEY": "${XUN_TOY_KEY}"}, "call_timeout": 60}}}}}

Each server's tools become `mcp_<server>__<tool>`; `mcp_refresh` re-reads one server.
Settings absent -> nothing happens. A server that will not start is reported and
skipped; the others still load. See README.md for what v1 does not do.
"""
from __future__ import annotations

import re
from typing import Any

import rich

from xun import Command, ExtensionContext, Result, ToolBox, ToolCallContext, tool_attr, extension_attr
from xun.error_catch import except_safe
from xun.toolcall import Function
from xun.tools.cmd import truncate_output
from xun.tools.common import defer_tool_image
from xun.types import ErrorInfo

from .client import MANAGER, McpError

EXT_NAME = "mcp"
MAX_TEXT = 16_000            # same budget xun's own cmd tool uses for output
CALL_TIMEOUT = 120.0
NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
_REQUIRED_API = ("register_raw", "pop", "is_disabled")


def _prefix(server: str) -> str:
    return f"mcp_{server}__"


def _tool_name(server: str, tool: str) -> str:
    """Provider-safe name: `[a-zA-Z0-9_-]{1,64}`."""
    return NAME_CHARS.sub("_", f"{_prefix(server)}{tool}")[:64]


def _servers(ctx: ExtensionContext | ToolCallContext) -> dict[str, dict]:
    """Configured servers, keyed by name, with unusable entries reported and dropped."""
    bag = ctx.agent.config.extension_settings.get(EXT_NAME, {})
    declared = bag.get("servers", {}) if isinstance(bag, dict) else {}
    assert isinstance(declared, dict), "Declared MCP servers must be a dictionary"
    servers: dict[str, dict] = {}
    for name, cfg in (declared or {}).items():
        if not isinstance(cfg, dict) or not isinstance(cfg.get("command"), str):
            rich.print(f"[yellow]MCP warning:[/yellow] skipping server '{name}': "
                       f"needs a string 'command'")
            continue
        servers[name] = cfg
    return servers


def _transport_kwargs(cfg: dict) -> dict:
    """Everything but `call_timeout`, which belongs to the wiring layer, not the pipe.

    The remaining keys go straight into the transport constructor, so the config surface
    and the constructor stay one thing.
    """
    return {k: v for k, v in cfg.items() if k != "call_timeout"}


def _timeout(cfg: dict) -> float:
    """Per-tool-call budget for this server; a hung server must not hang the agent."""
    value = cfg.get("call_timeout", CALL_TIMEOUT)
    return float(value) if isinstance(value, (int, float, str)) else CALL_TIMEOUT


def _prune(kwargs: dict, schema: dict) -> tuple[dict, list[str]]:
    """Split arguments into (usable, dropped).

    Many servers (FastMCP-generated ones included) publish additionalProperties:false and
    reject unknown keys, while models like to invent them. Open schemas are passed through
    untouched: there the server is the authority.
    """
    props = set((schema.get("properties") or {}).keys())
    if schema.get("additionalProperties", True) is False and props:
        kept = {k: v for k, v in kwargs.items() if k in props}
        return kept, sorted(set(kwargs) - props)
    return kwargs, []


def _to_result(res: dict, ctx: ToolCallContext, tool: str) -> Any:
    """Map an MCP CallToolResult onto a JSON-serializable xun tool result."""
    texts: list[str] = []
    extra: dict[str, Any] = {}
    for block in res.get("content", []) or []:
        kind = block.get("type")
        if kind == "text":
            texts.append(block.get("text", ""))
        elif kind == "image" and block.get("data"):
            mime = block.get("mimeType", "image/png")
            defer_tool_image(ctx, f"data:{mime};base64,{block['data']}", msg=f"[MCP {tool}]")
            texts.append("[image attached]")
        else:
            extra.setdefault("content", []).append(block)
    if res.get("structuredContent") is not None:
        extra["structuredContent"] = res["structuredContent"]

    joined = truncate_output("\n".join(texts), MAX_TEXT)
    out: Any = {"text": joined, **extra} if extra else joined
    if res.get("isError"):
        return Result.Err(ErrorInfo(error=f"MCP tool {tool} failed", details=joined[:2000]))
    return out


def _make_tool(server: str, spec: dict, cfg: dict) -> Function:
    tool = spec["name"]
    schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
    name = _tool_name(server, tool)
    hints = spec.get("annotations") or {}
    description = (spec.get("description") or f"MCP tool {tool} on {server}.").strip()
    description += "\n[read-only]" if hints.get("readOnlyHint") else ""
    description += "\n[destructive]" if hints.get("destructiveHint") else ""

    @tool_attr(name=name)
    def _impl(ctx: ToolCallContext, **kwargs: Any) -> Any:
        args, dropped = _prune(kwargs, schema)
        if dropped:
            ctx.agent.info(f"[MCP {tool}] dropped undeclared arguments: {dropped}")
        MANAGER.ensure(server, _transport_kwargs(cfg))
        return _to_result(MANAGER.call(server, tool, args, _timeout(cfg)), ctx, name)

    _impl.__doc__ = description
    # args_model=None keeps inputSchema verbatim: pydantic cannot express anyOf/$ref,
    # and round-tripping a schema through Python types and back is a lossy conversion.
    # The server is the validator; a bad argument costs one round-trip to be reported.
    return Function(
        func=except_safe(_impl), name=name, description=description, args_model=None,
        tool_schema={"type": "function", "function": {
            "name": name, "description": description, "parameters": schema}},
        context_param="ctx", required_capabilities=set(), override=False)


def _reconcile(box: ToolBox, prefix: str, wanted: dict[str, Function]) -> tuple[int, int, int]:
    """Make the `prefix*` namespace equal to what the server reports.

    - gone from the server -> pop (its disabled mark goes with it, no poisoned names)
    - new                  -> register_raw
    - schema changed       -> pop + register_raw, then restore the user's disabled mark
    - unchanged            -> untouched, so whatever the user switched off stays off

    `setup_extension` replays onto cloned toolboxes, and `register_raw` rejects a name
    already held, so this must run before anything is registered.
    """
    added = replaced = removed = 0
    known = {t.name: t for t in box.list_tools(include_disabled=True)
             if t.name.startswith(prefix)}
    for name in [n for n in known if n not in wanted]:
        box.pop(name)
        removed += 1
    for name, fn in wanted.items():
        held = known.get(name)
        if held is None:
            box.register_raw(fn)
            added += 1
        elif held.tool_schema == fn.tool_schema:
            continue
        else:
            was_off = box.is_disabled(name)
            box.pop(name)
            box.register_raw(fn)
            if was_off:
                box.disable(name)
            replaced += 1
    return added, replaced, removed


def _wanted(server: str, specs: list[dict], cfg: dict) -> dict[str, Function]:
    return {_tool_name(server, s["name"]): _make_tool(server, s, cfg)
            for s in specs if isinstance(s, dict) and s.get("name")}


@tool_attr(name="mcp_refresh", override=True)
def mcp_refresh(ctx: ToolCallContext, server: str) -> Any:
    """Re-read one MCP server's tool list and sync it into this agent's tools."""
    cfg = _servers(ctx).get(server)
    if cfg is None:
        return f"no server '{server}' in {EXT_NAME} settings"
    try:
        specs = MANAGER.ensure(server, _transport_kwargs(cfg), refresh=True)
    except McpError as exc:
        return Result.Err(ErrorInfo(error=f"MCP server {server} refresh failed",
                                    details=str(exc)))
    added, replaced, removed = _reconcile(
        ctx.agent.toolbox, _prefix(server), _wanted(server, specs, cfg))
    return {"server": server, "added": added, "replaced": replaced, "removed": removed}


@extension_attr(api_min_version="1.3.0")
def setup_extension(ctx: ExtensionContext) -> None:
    box = ctx.agent.toolbox
    missing = [m for m in _REQUIRED_API if not hasattr(box, m)]
    if missing:
        rich.print(f"[yellow]MCP skipped:[/yellow] this xun is older than the extension: "
                   f"no ToolBox.{' / ToolBox.'.join(missing)}. "
                   f"Install the matching mcp extension or upgrade xun.")
        return

    servers = _servers(ctx)
    if not servers:
        return

    loaded = 0
    for server, cfg in servers.items():
        try:
            specs = MANAGER.ensure(server, _transport_kwargs(cfg))
        except McpError as exc:                    # one bad server must not lose the rest
            rich.print(f"[yellow]MCP warning:[/yellow] {exc}")
            continue
        wanted = _wanted(server, specs, cfg)
        _reconcile(box, _prefix(server), wanted)
        loaded += len(wanted)

    box.register(mcp_refresh)
    def command(agent: Any, args: list[str]) -> None:
        configured = _servers(ctx)
        if args == ["status"]:
            agent.info("\n".join(f"{name}: {MANAGER.status(name)}"
                                 for name in sorted(configured)))
            return
        if len(args) == 2 and args[0] in {"status", "restart"}:
            name = args[1]
            if name not in configured:
                raise ValueError(f"No configured MCP server '{name}'")
            if args[0] == "status":
                agent.info(f"{name}: {MANAGER.status(name)}")
                return
            specs = MANAGER.restart(name, _transport_kwargs(configured[name]))
            added, replaced, removed = _reconcile(
                agent.toolbox, _prefix(name), _wanted(name, specs, configured[name]))
            agent.info(f"{name}: restarted (added {added}, replaced {replaced}, "
                       f"removed {removed})")
            return
        raise ValueError("Usage: /mcp status [server] | /mcp restart <server>")

    ctx.agent.command.register(Command(
        "mcp", command, "Show MCP status or restart a server.",
        "Usage: /mcp status [server] | /mcp restart <server>"))
    if loaded:
        rich.print(f"[green]MCP:[/green] {loaded} tools from {len(servers)} server(s) "
                   f"on '{ctx.agent.name}'")
