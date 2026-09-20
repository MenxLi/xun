# Xun

A mini LLM agent framework with function-based tools and sub-agent spawning.

The core codebase is compact: about 3000 lines in `src/xun/*.py` (mostly hand written), with comprehensive type hints.

<!-- 
<details>
<summary>Why this name?</summary>

取此名称有两点考虑。其一是技术含义：智能体的执行过程本身即一种搜索，在可
用工具与不断变化的会话状态中检索、试探与回退，直至任务收敛，模型侧的优化
过程同样可作此理解；其同音字“询”对应以对话问询驱动的交互方式，“训”对应以
指令与工具配置约束并扩展智能体行为的方式。

其二是使用便利：xun 为单音节三字母拼音，在命令行中无需切换输入法即可连续
键入，符合本软件以命令行与终端交互为主要入口的使用习惯。

此外，本人名字中也包含“寻”字，这个名称对我个人也有特殊意义 :D

</details>
-->


## Quick Start

Requires Python 3.12+ (PEP 695)

```bash
# 1. Install the package
pip install xun-agent

# 2. Install Playwright browsers (if using the default browser tools)
playwright install

# 3. Configure environment variables (see `Configuration` section below)
vim .env

# 4. Run the agent in interactive mode
xun

# (OR) Run the agent in web mode
xuns .
```

`xuns` accepts at most one workspace directory: every session shares it, 
or each session gets its own temporary workspace if omitted. 

<details>
<summary>Installation from source</summary>

```sh
git clone https://github.com/MenxLi/xun.git
cd xun
make build-web
pip install .
```
</details>

## Usage


**Basic**: Quickly set up an agent with plain functions as tools — no decorators, no classes needed. 

```python
from xun import setup_agent

def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

agent = setup_agent(tools = [add])
agent.instruct("Add 2 and 3.").execute()
```

**Advanced**: The framework is flexible and extensible.
Additional features are shown in [demo.ipynb](demo.ipynb), including:
- `Agent` configuration
- Display extension
- Output validation
- Tool attributes
- Context injection
- Type-state transition
- Sub-agent spawning
- Lifecycle hooks
- ...

Do check out [demo.ipynb](demo.ipynb) for detailed examples. 

## CLI

Run `xun` in your terminal to start an interactive session.
You can also pass a prompt as an argument to begin with a specific instruction.
```bash
xun "Write a hello world python script and save it to hello.py"
```

Image attachments are supported in the format of `[image:path_or_url]`. For example:
```
>>> [image:cat.png image:https://example.com/dog.png] compare them.
```

Input `/help` to see the full list of commands.

## Web

`web_session` starts the same managed web experience available through `xuns`:

```python
from xun import web_session

web_session(workdir=".", base_path="/xun", manage_sessions=True)
```

`WebDisplay` provides lower-level access to the interactive web interface for custom service composition.
It can be used as a chat-based web application, or as a backend for other applications.

```python
from xun import WebDisplay, WebDisplayService, setup_agent

display = WebDisplay(expose_files=True)
agent = setup_agent(display=display, default_tools=True)
service = WebDisplayService().mount("/", display)
service.start(blocking=True)
```

Open any tokenized URL printed at startup; the query token is exchanged for an HttpOnly cookie, so the browser reaches every mounted display without logging in again. API clients can use `Authorization: Bearer <token>`. File browsing, upload, download, and deletion require `expose_files=True`.

Multiple displays can share one authenticated service, each keeping its own agents, event history, and file policy:

```python
service = WebDisplayService()
service.mount("/research", research_display)
service.mount("/coding", coding_display)
service.start(blocking=True)
```

`display.build_routes()` and `display.build_app()` do not add authentication — use `WebDisplayService`, or provide your own in a custom ASGI host.

## Docker

```bash
# first clone the repository
git clone https://github.com/MenxLi/xun.git
cd xun

make build-docker   # builds the web frontend, then the `xun` image

xunc                # sandbox: temporary workspace inside the container
xunc .              # bind mount the current directory as /workspace
xunc --copy .       # copy the current directory into /workspace instead
```

`xunc` runs `xuns --host 0.0.0.0` in the container and publishes port 18960 (bridge mode), so the web UI is reachable from the host at the tokenized URL printed at startup. The image fixes `XUN_HOME=/.xun`, and the host xun home is always copied into it (following symlinks, so a `.xun/extensions` symlink into a repo `extensions/` dir is carried in) — start from an empty directory to run without one. Other options: `--exec CMD` (e.g. `--exec bash`), `--port LIST`, `--network host` (avoid on macOS — not reachable from a host browser), `--env PATTERNS` (extra env vars to forward; `XUN_*`/`_XUN_*` are always forwarded except `XUN_HOME`), `--image` / `--name`.

### Multiplexed server

`xunx` runs one persistent container per registered user, proxied through one public server:

```bash
xunx user-add alice   # prints the access token
xunx user-list
xunx user-del alice   # the running container is stopped within one reconcile interval
xunx serve --host 0.0.0.0 --port 18960 --port-range 20000-20100

xunx upgrade alice    # recreate her container from the current image (or --all)
```

Open `http://localhost:18960/alice?token=TOKEN`. Users live in `$XUN_HOME/x/xunx.db`; container ports are drawn randomly from `--port-range` and bound to host loopback only. 
Containers outlive `serve` shutdown and are re-adopted (keeping in-container sessions alive) on the next start; containers left for deleted users are pruned then. 
Containers are named `xunx-<instance>-<user>`, so use `docker ps` / `docker logs` to inspect them directly. 
`xunx upgrade` only records intent — the running `serve` process recreates flagged containers on its next reconcile, and recreation discards in-container data (workspace, saved conversations). 
`XUN_*`/`_XUN_*` env vars except `XUN_HOME` are forwarded into each container. The host xun home is copied into each container's `/.xun` (config, extensions; follows symlinks) — note this shares it across all users.

<details>
<summary>Frontend development</summary>
The frontend development command starts both the backend and Vite with Vue DevTools:

```bash
cd web
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. Build a production bundle with `npm run build`. See [web/README.md](web/README.md) for connecting the UI to a separately managed backend.
</details>

## Configuration

xun reads optional configuration from `.xun/config.json` (override the location with `XUN_HOME`); missing fields fall back to built-in defaults. Include only the fields you want to change, for example to override the model:

```json
{
    "model": {
        "name": "my-model"
    }
}
```

The config supports `${XUN_...}` placeholders which are substituted from environment variables (e.g. `${XUN_OPENAI_API_KEY}`), so secrets can live in a `.env` file instead. 

| Config field | Environment variable | Description |
|---|---|---|
| `provider.openai_base_url` | `${XUN_OPENAI_BASE_URL}` | OpenAI-compatible API endpoint. |
| `provider.openai_api_key` | `${XUN_OPENAI_API_KEY}` | API key. |
| `model.name` | `${XUN_OPENAI_MODEL}` (empty) | Model identifier. If the resolved value is empty, available models are auto-detected from the API. |

More configuration options are available; see the source code at [src/xun/config.py](src/xun/config.py).

## Extensions

Drop a Python file under `$XUN_HOME/extensions/` and every agent picks up its effects (tools, hooks, commands, config tweaks) at initialization — no wiring needed. Extensions are trusted code, like a shell rc file.

Two source forms, each yielding an extension named `{name}`:

- `extensions/{name}/setup_extension.py` — package form, supports relative imports of helper modules
- `extensions/{name}.py` — flat single file, for the common few-lines-of-hooks case

The entry function has a fixed name and receives the agent being initialized:

```python
"""Log every tool call."""
from xun import ExtensionContext

def setup_extension(ctx: ExtensionContext) -> None:
    ctx.agent.hooks.before_tool_call.add(lambda args: print(args.tool_calls))
```

The module docstring (first line) becomes the extension's description, listed by the `/extensions` command. Failing extensions warn and never block startup. See [extensions/z_search](extensions/z_search) for a real example that overrides the built-in `web_search` tool. Set `config.enable_extensions = False` to opt out (internal helper agents do this automatically).