# Xun

A mini LLM agent runtime, with ready-to-use components for building agent systems.
Main features include:
- Lightweight and compact core codebase (less than 5000 lines in `src/xun/*.py`, with comprehensive type hints). 
- Ready-to-use components like predefined tools, commands, extensions, and CLI/web interfaces. 
- Extensible architecture for customization. 

<!-- The core codebase is compact: 
less than 5000 lines in `src/xun/*.py` (direct descendant of `src/xun`), 
mostly hand written, with comprehensive type hints. -->

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

# 3. Configure environment variables (details see `Configuration` section below)
export XUN_OPENAI_BASE_URL="..."
export XUN_OPENAI_API_KEY="..."
export XUN_OPENAI_MODEL="..."

# 4. Run the agent in interactive mode
xun

# (OR) Run the agent in web mode
xuns .
```

<details>
<summary>Installation from source</summary>

```sh
git clone https://github.com/MenxLi/xun.git
cd xun
make build-web
pip install .
```
</details>

## Documentation

This README and `demo.ipynb` provide brief overviews of Xun and its features.

If you want more detailed documentation, please visit the web interface provided by `xuns`.

Opens the web application without LLM configured (If you don't want to set up LLM):
```sh
xuns --no-initial-agent
```

Then visit: `http://localhost:18960/docs/`

> **Documentation is included in PyPI releases.** Start `xuns/xunc` to browse the
> bilingual site at `/docs/`. Need a refresh? Let Xun write its own bilingual
> manual; see [Building Documentation](README-BUILD-DOCS.md).

## Session Entrypoints

As mentioned above, Xun provides multiple entrypoints for different usage scenarios: 

| Command | Form | Use case |
|---|---|---|
| `xun` | Terminal session | Work interactively in the current directory. |
| `xuns` | Web service | Use the managed browser experience. |
| `xunc` | Container session | Run `xuns` in an isolated Docker container. |
| `xunx` | Multiplexed service | Proxy per-user containers through one server. |

The agent respects `AGENTS.md` at its working directory, 
to turn off this behavior, set `load_project_instructions: false` in the agent's configuration.

## API

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

**Do check out [demo.ipynb](demo.ipynb) for detailed examples.**

<details>
<summary>Why APIs are all synchronous?</summary>

The APIs are designed to be synchronous deliberately, for the following reasons:
- Simplifies the mental model for users: synchronous code is easier to type and reason about.
- Synchronous API does not mean inefficient: the threaded model still optimizes underlying I/O operations.
- The bottleneck of an agent system usually comes from the underlying LLM call... which are un-scalable anyway.

</details>

## CLI

Run `xun` in your terminal to start an interactive session.
The current workspace is used as the working directory.
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

`xuns` starts the managed web service for Xun, providing a browser-based interface to interact with the system.

If no arguments are provided, `xuns` will start the web service with a temporary workspace.
Otherwise, the first argument is treated as the path to the workspace directory.

```sh
xuns        # start with a temporary workspace
xuns .      # start with the current directory as the workspace
```
The url with access token will be displayed in the terminal after starting the web service.

Inside web interface, you can manage sessions. 
`xuns` accepts at most one workspace directory: every session shares it, 
or each session gets its own temporary workspace if omitted. 

## Docker

To use `xunc` and `xunx`, you need to build the Docker image first.

```bash
# first clone the repository
git clone https://github.com/MenxLi/xun.git
cd xun

make build-docker   # builds the web frontend, then the `xun` image

xunc                # sandbox: temporary workspace inside the container
xunc .              # bind mount the current directory as /workspace
xunc --copy .       # copy the current directory into /workspace instead
xunc --exec "xun"   # starting the `xun` cli session instead of web
```

`xunx` uses the same image to run one persistent container per user behind a
single gateway:

```bash
xunx user-add alice
xunx serve --host 0.0.0.0 --port 18960
```

Pause a user's container without discarding its state, then resume it later:

```bash
xunx pause alice
xunx resume alice
```

> Note that `xunx` starts container as temporary per-user instances.
> Stopping `xunx` won't stop the container, 
> but if you do `xunx upgrade <username>` or restart docker daemon, the temporary containers will be removed.

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
| `auto_confirm` | `${XUN_AUTO_CONFIRM}` | Automatically confirm actions without prompting. |
| `enable_extensions` | — | Load extensions from `$XUN_HOME/extensions/`. Internal helper agents set this to `false` automatically. |
| `load_project_instructions` | — | Automatically load `AGENTS.md` from the working directory into the system prompt. |
| `extension_settings` | — | Per-extension settings, keyed by extension name; each extension reads its own through `ctx.settings` (see Extensions). |

More configuration options are available; see the source code of `AgentConfig` at [src/xun/config.py](src/xun/config.py).

## Extensions

**Extension is the best way to customize and extend xun's functionality.**

Drop a Python file under `$XUN_HOME/extensions/` and every agent picks up its effects (tools, hooks, commands, config tweaks) at initialization — no wiring needed. Extensions are trusted code, like a shell rc file.

```python
"""Log every tool call."""  # extensions/<name>.py
from xun import ExtensionContext

def setup_extension(ctx: ExtensionContext) -> None:
    ctx.agent.hooks.before_tool_call.add(lambda args: print(args.tool_calls))
```

See [extensions/README.md](extensions/README.md) for the full mechanics.

## Skills and MCP

Two ready-made extensions live under `extensions/`, each opt-in by copying it into `$XUN_HOME/extensions/`:

**Skills** discovers standard [Agent Skills](https://agentskills.io) `SKILL.md` folders from `$XUN_HOME/skills/`, `<workdir>/.agents/skills/`, and `~/.agents/skills/`. Install its dependency with `pip install pyyaml`. 
It progressively discloses instructions through `activate_skill`, exposes bundled files and scripts only after activation, and adds `/skills` for listing and activation.

**MCP** lets xun communicate with [Model Context Protocol](https://modelcontextprotocol.io) servers and expose their tools as xun tools. 
It needs no extra dependencies; configure servers in `config.json` under `extension_settings.mcp`. 
See [extensions/mcp/README.md](extensions/mcp/README.md) for full details.

<details>
<summary>Why are these extensions rather than core?</summary>

- **They fit the mechanism.** Both only consume core primitives — tools, commands, prompt sections, settings — and neither is wanted by every user.
- **They track fast-moving standards.** Agent Skills and MCP are de facto standards still in flux; as extensions, a spec change is a drop-in file swap, not a core release.
- **Core stays compact.** The two double as proof that the extension system suffices for real features.

</details>
