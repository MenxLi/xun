# MCP Extension

Talks to [Model Context Protocol](https://modelcontextprotocol.io) servers as a **client**
and exposes their tools as xun tools. It is an extension, not core: an agent harness is
just another MCP client, and nothing MCP-specific belongs in xun's core.

No dependencies — the client is stdlib-only (`asyncio` + `json` in a background thread,
because xun's own API is synchronous).

## Install

Copy this directory to `$XUN_HOME/extensions/mcp/`. It needs a xun build that has
`ToolBox.register_raw`, `ToolBox.pop`, `ToolBox.is_disabled` and
`AgentConfig.extension_settings` — all on main after commit `2ef8fd369ce9` 
(Will be ready on v1.3.0 release).

## Configure

Servers are declared in `config.json` under `extension_settings.mcp`:

```json
{
  "extension_settings": {
    "mcp": {
      "servers": {
        "github": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-github"],
          "env": { "GITHUB_TOKEN": "${XUN_GITHUB_TOKEN}" },
          "init_timeout": 20,
          "call_timeout": 120
        }
      }
    }
  }
}
```

`command` is required; `args`, `env`, `cwd`, `init_timeout` (default 20s) and
`call_timeout` (default 120s) are optional. Every key except `call_timeout` is handed
straight to the transport constructor, so the config surface and the constructor stay one
thing. No settings, or an empty `servers`, means the extension does nothing at all.

Two rules about `$` — the whole config file goes through `string.Template`:

- `${XUN_ANYTHING}` is substituted from the environment, which is how you keep tokens out
  of the config text. Placeholders **must** start with `XUN_` and must exist, otherwise
  startup fails loudly.
- Any other `$name` is treated as a placeholder too, so a literal `$HOME` in `args` or
  `env` must be written `$$HOME`.

`xunc` copies `config.json` into every tenant container, so per-user secrets do not belong
in `servers.*.env` in a multi-tenant deployment.

### Example: Blender

> Prerequisites: Blender and the `mcp-for-blender` addon installed, plus `uvx` available in your PATH

Blender's `mcp-for-blender` server speaks stdio and bridges to a socket inside Blender
(default `localhost:9876`), so it drops straight into the transport above:

```json
{
  "extension_settings": {
    "mcp": {
      "servers": {
        "blender": {
          "command": "uvx",
          "args": ["mcp-for-blender"],
          "env": { "BLENDER_HOST": "localhost", "BLENDER_PORT": "9876" },
          "init_timeout": 10,
          "call_timeout": 300
        }
      }
    }
  }
}
```

Install the addon it talks to with `uvx mcp-for-blender install-addon`, enable
"Interface: MCP for Blender", and run Blender with a GUI — headless (`blender -b`) never
runs the command handler. 

## What you get

- Each server's tools appear as `mcp_<server>__<tool>`, with the server's `inputSchema`
  copied **verbatim** into the tool schema — `anyOf`, `$ref`, `format`, nested objects and
  `additionalProperties` all survive. Sub-agents inherit them for free, and one
  `toolbox.disable("mcp_github__*")` call switches a whole server off.
- `mcp_refresh(server)` re-reads one server's tool list and reports
  `{"added": …, "replaced": …, "removed": …}`.
- Re-initialization is idempotent: an unchanged tool is left alone (so a tool you disabled
  stays disabled), a changed schema is replaced with your disable-flag restored, and a tool
  the server dropped is removed without leaving a stale flag behind.
- Results: text (truncated at 16k chars like xun's own shell tool), `structuredContent`
  preserved, `image` blocks attached to the conversation rather than stuffed into the tool
  result, and `isError` returned as a real failure so hooks and the UI treat it as one.
- A server that will not start is reported and skipped; others still load, and the failure
  is retried at most once per 30s so one broken server does not tax every sub-agent.

## Deliberately not implemented

- **Remote transports** (`streamable-http`, SSE) and OAuth. When that is needed, wrapping
  `fastmcp.Client` is the cheaper path — measured in the design notes, the hand-written
  stdio client is 185 lines and saves 46 packages.
- **Resources and prompts** as first-class concepts.
- **Elicitation, sampling, progress, and `tools/list` change notifications.** Server →
  client requests need a "which agent is asking" routing answer that v1 does not have, and
  on protocol `2026-07-28` both elicitation and `list_changed` changed shape (clients must
  opt in via `subscriptions/listen`). v1 sticks to `initialize` / `tools/list` /
  `tools/call`, which every protocol version agrees on. The visible cost is that a tool
  removed at runtime stays callable until `mcp_refresh`, and the call just reports an error.
- **Token budget.** Tool schemas are sent on every request: 30 tools cost roughly 5–7k
  tokens per call, so a 100-tool server is a real cost. If that bites, register a summary
  and register the schemas lazily, the way the skills extension discloses instructions.

## Tests

`test/test_mcp_extension.py` (runs with the normal suite, `make test`). Most cases fake
the transport; one class spawns `test/mcp_test_server.py` as a real stdio server. Verified on
Linux; the Windows event-loop path (`ProactorEventLoop` in a worker thread) is untested.
