# Xun

An autonomous, mini LLM agent with tooling for file manipulation, command execution, web search, browser automation, and sub-agent spawning.

<details>
<summary>Why this name?</summary>

**Xun** has multiple relevant meanings in Chinese, all pronounced the same way but written with different characters and have meanings that align well with the purpose of this project:

| Character | Pinyin | Meaning | Why it fits |
|---|---|---|---|
| **寻** | *xún* | seek, search | Agents that seek information and solutions for you |
| **讯** | *xùn* | message, information | Agents that process information and communicate with you |
| **训** | *xùn* | train, instruct | A extensible framework that can be tuned with new tools and instructions |

Pronounced like *shoon* — short, simple, and easy to type.

Also drawn from the author's given name (Meng-Xun), as a personal touch to this project :)

</details>

## Quick Start

```bash
# 1. Install dependencies
pip install git+https://github.com/MenxLi/xun.git

# 2. Install Playwright browsers
playwright install

# 3. Configure environment variables (see `Configuration` section below)
vim .env

# 4. Run the agent in interactive mode
xun
```

## Features

![Features](https://limengxun-public-1322620498.cos.ap-guangzhou.myqcloud.com/images/260504-xun-3jyR8sMPHP.png)

Image attachments are supported in the format of `[image:path_or_url]`. For example:
```
>>> [image:cat.png image:https://example.com/dog.png] compare them.
```

Input `.help` to see the full list of commands.

<details>
<summary>Interactive Commands</summary>

- **`.help`** — Show help message.
- **`.restart`** — Clear conversation history and restart.
- **`.retry`** — Retry the last user message.
- **`.revise`** — Re-input the last user message.
- **`.tools`** — List registered tools and their descriptions.
- **`.config`** — Show current API configuration.
- **`.condense`** — Condense conversation history into a summary to save context.
- **`.dump`** — Dump conversation history to a JSON file.
- **`.load`** — Load conversation history from a JSON file (defaults to the latest).
- **`.history`** — Show conversation history in the terminal.
- **`.exit`** — Exit the program.

</details>

## Configuration

xun uses environment variables, preferably stored in a `.env` file.

| Variable | Default | Description |
|---|---|---|
| `XUN_OPENAI_BASE_URL` | `http://<host-ip>:8000/v1` | OpenAI-compatible API endpoint. Default to port 8000 from localhost or docker container host. |
| `XUN_OPENAI_API_KEY` | *(empty)* | API key. |
| `XUN_OPENAI_MODEL` | *(empty)* | Model identifier. If empty, will auto-detect available models from the API. |
| `XUN_AUTO_CONFIRM` | `false` | Auto-approve actions without prompting. |
