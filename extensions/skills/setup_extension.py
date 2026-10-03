"""Agent Skills support: hand the model task-specific instruction bundles.

`SKILL.md` bundles (https://agentskills.io) are discovered under `$XUN_HOME/skills`,
`<workdir>/.agents/skills` and `~/.agents/skills`, and exposed as the `/skills`
command plus the `list_skills` / `activate_skill` / `read_skill_file` /
`run_skill_script` tools. Only `/skills` is registered against an empty catalog, so
that `/skills reload` can pick up a bundle created after this agent started.
"""
import os
from typing import Any, Callable

from xun import Command, ExtensionContext, ToolCallContext
from xun.display_abstract import ShowExtensionsEvent
from xun.toolcall import ToolAttr

from .catalog import Skill, refresh, require, roots, session, skills
from .tools import activate_skill, list_skills, read_skill_file, run_skill_script


def tool_funcs() -> list[Callable]:
    """The model-facing tools this extension offers on the current platform."""
    funcs: list[Callable] = [list_skills, activate_skill, read_skill_file]
    if os.name != "nt":
        funcs.append(run_skill_script)  # argv-based execution, POSIX only
    return funcs


def sync_tools(agent: Any, available: list[Skill]) -> None:
    """Put the skill tools on the toolbox while `available` is non-empty.

    Additive and idempotent - present names are left alone, so nothing collides and
    a tool the user disabled with `/tool` stays disabled - because it runs again on
    every `/skills reload` and on the setup replay of a sub-agent, whose toolbox is
    the parent's clone. An empty catalog registers nothing.
    """
    if not available:
        return
    present = {tool.name for tool in agent.toolbox.list_tools(include_disabled=True)}
    agent.toolbox.register(*[f for f in tool_funcs() if _tool_name(f) not in present])


def _tool_name(func: Callable) -> str:
    """The name `func` registers under: its `tool_attr` override, else its `__name__`."""
    attr = ToolAttr.extract_from(func)
    return (attr.name if attr and attr.name else None) or func.__name__


def _discovery_hint(agent: Any) -> str:
    """Where to drop a bundle so that `/skills reload` finds it."""
    roots_hint = "\n".join(f"  {root}  ({source})" for root, source in roots(agent))
    return ("No skills discovered. Add a directory holding a `SKILL.md` whose frontmatter "
            f"sets `name` and `description` under one of:\n{roots_hint}\nThen run `/skills reload`.")


def setup_extension(ctx: ExtensionContext) -> None:
    agent = ctx.agent
    sync_tools(agent, skills(agent)[0])
    refresh(agent)

    def command(agent: Any, args: list[str]) -> None:
        """List, inspect, activate, or reload discovered skills.

        With no arguments, list each discovered skill with its source and current
        activation status, or name the roots that were searched when there are none.
        `info` shows one skill's metadata and bundled files without activating it.
        `activate` loads its instructions and refreshes the system prompt catalog.
        `reload` rescans the roots, which is how a bundle created after this agent
        started gains its tools.

        Usage:
            /skills                  # list discovered skills
            /skills info <name>      # inspect <name> without activating it
            /skills activate <name>  # activate <name> and show its instructions
            /skills reload           # rescan skill directories
        """
        if not args:
            available, issues = skills(agent)
            if not available:
                agent.info(_discovery_hint(agent))
            if available or issues:
                records = [type("Info", (), {"name": s.name, "description": s.description, "status": "loaded" if s.name in session(agent).active else "ready", "reason": None, "source": s.source}) for s in available]
                records += [type("Info", (), {"name": name, "description": "", "status": "failed", "reason": reason, "source": None}) for name, reason in issues]
                agent.display_event(ShowExtensionsEvent.from_infos(records, title="Skills"))
            return
        if args == ["reload"]:
            available, issues = skills(agent)
            sync_tools(agent, available)
            refresh(agent)
            summary = f"Skills reloaded: {len(available)} available"
            if issues:
                summary += f", {len(issues)} unusable ({', '.join(name for name, _ in issues)})"
            agent.info(summary + ".")
            return
        if len(args) != 2 or args[0] not in {"activate", "info"}:
            raise ValueError("Usage: skills [info <name> | activate <name> | reload]")
        if args[0] == "activate":
            agent.info(activate_skill(ToolCallContext(agent, "activate_skill", None), args[1]))
            return
        skill = require(agent, args[1])
        files = "\n".join(f"  {file}" for file in skill.files()) or "  (none)"
        agent.info(
            f"Name: {skill.name}\nDescription: {skill.description}\nSource: {skill.source}\n"
            f"Directory: {skill.directory}\nActive: {'yes' if skill.name in session(agent).active else 'no'}\n"
            f"Bundled files:\n{files}"
        )

    agent.command.register(Command("skills", command, "List, inspect, activate, or reload skills."))
