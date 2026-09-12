# Xun

A mini LLM agent framework with function-based tools and sub-agent spawning.

<!-- 
Some of the design philosophy:
- Plain functions as tools — no decorators, no classes needed
- Compact core — no heavy abstractions, full type hints
- Sub-agent spawning — built-in multi-agent orchestration
- Event-driven display — pluggable I/O via a simple interface
- Context injection — pass execution context into tools seamlessly 
-->

The core codebase is compact: about 3000 lines in `src/xun/*.py`, with comprehensive type hints.

<!-- <details>
<summary>Why this name?</summary>

**Xun** has multiple relevant meanings in Chinese, all pronounced the same way but written with different characters and have meanings that align well with the purpose of this project:

| Character | Pinyin | Meaning | Why it fits |
|---|---|---|---|
| **寻** | *xún* | seek, search | Agents that seek information and solutions for you |
| **讯** | *xùn* | message, information | Agents that process information and communicate with you |
| **训** | *xùn* | train, instruct | A extensible framework that can be tuned with new tools and instructions |

Pronounced like *shoon*: short, simple, and easy to type.

Also drawn from the author's given name (Meng-Xun), as a personal touch :)

</details>  -->


## Quick Start

Requires Python 3.12+ (PEP 695)

```bash
# 1. Install dependencies
pip install git+https://github.com/MenxLi/xun.git

# 2. Install Playwright browsers (if using the default browser tools)
playwright install

# 3. Configure environment variables (see `Configuration` section below)
vim .env

# 4. Run the agent in interactive mode
xun
```

Optionally, run the agent in web mode: 
```sh
# - Build the web frontend (if using the web display)
make build-web
# - Start the web server at current directory
xuns .
# - Use a temporary workspace instead
xuns
# - Disable creating and removing sessions from the UI
xuns . --no-manage-sessions
```

`xuns` accepts at most one workspace directory. Session management is enabled by default. When a directory is supplied, every session uses it; otherwise, each session gets an independent temporary workspace that is removed when the session ends.

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

Open any tokenized URL printed at startup. The service exchanges its query token for one HttpOnly cookie scoped to `/`, so the browser can access every mounted display without logging in again. API clients can use `Authorization: Bearer <token>`.

The shared UI is served once at `<base_path>/chat/`. Display backends are isolated below `<base_path>/session/`: with `base_path="/xun"`, a display mounted logically at `/` is available at `/xun/session/`, while `/research` is available at `/xun/session/research/`. Session management and login are similarly scoped below `/xun/api/sessions` and `/xun/login`. Selecting a session updates the UI's connection without reloading the page.

File browsing, upload, download, and deletion are disabled unless `expose_files=True`. 
The agent will start in web mode, and you can access it via the printed URL.

Multiple displays can share one authenticated service. Each mount keeps its own agents, event history, and file policy:

```python
service = WebDisplayService()
service.mount("/research", research_display)
service.mount("/coding", coding_display)
service.start(blocking=True)
```

`display.build_routes()` and `display.build_app()` do not add authentication. Use `WebDisplayService` for the authenticated server, or provide authentication and lifecycle handling in your own ASGI host.

## Docker

Build the image and run the agent in a container with `xunc`:

```bash
make build-docker   # builds the web frontend, then the `xun` image

xunc                # sandbox: no host mount; xuns creates a temporary workspace in the container
xunc .              # mount the current directory as /workspace inside the container
xunc --copy .       # copy the current directory into /workspace (no host bind mount)
```

By default `xunc` starts `xuns --host 0.0.0.0` in the container and publishes port 18960 (`bridge` network), so the web UI is reachable from the host at the tokenized URL printed at startup. Options:

- `--exec CMD`: command to run inside the container (e.g. `--exec bash` for a plain shell, `--exec ""` for the image's default CMD)
- `--port LIST`: ports to publish in bridge mode (default `18960`)
- `--network host`: host networking (on macOS this is the Docker VM's network namespace, which is **not** reachable from a host browser — prefer the default bridge mode there)
- `--env PATTERNS`: extra environment variables to forward, comma-separated wildcards (`XUN_*` and `_XUN_*` are always forwarded)
- `--copy`: copy the positional directory into `/workspace` before starting instead of bind mounting it
- `--image` / `--name`: image (default `xun`) and container name

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

xun reads its configuration from `~/.xun/config.json` (the location can be overridden with the `XUN_HOME` environment variable). The file is **optional**: 
if it does not exist, built-in defaults are used, and no files are created.

The file only needs to contain the fields you want to change. For example, to just override the model:

```json
{
    "model": {
        "name": "my-model"
    }
}
```

The config supports `${XUN_...}` placeholders which are substituted from environment variables (e.g. `${XUN_OPENAI_API_KEY}`), so secrets can live in a `.env` file instead. A placeholder with no matching environment variable causes a startup error. 

| Config field | Built-in default | Description |
|---|---|---|
| `provider.openai_base_url` | `${XUN_OPENAI_BASE_URL}` | OpenAI-compatible API endpoint. |
| `provider.openai_api_key` | `${XUN_OPENAI_API_KEY}` | API key. |
| `model.name` | `${XUN_OPENAI_MODEL}` (empty) | Model identifier. If the resolved value is empty, available models are auto-detected from the API. |
| `model.capabilities` | `["vision"]` | Capabilities exposed to the model (e.g. `vision` for image input). |
| `auto_confirm` | `false` | Auto-approve actions without prompting. |
