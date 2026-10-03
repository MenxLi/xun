"""Agent Skills support: hand the model task-specific instruction bundles.

`SKILL.md` bundles (https://agentskills.io) are discovered under `$XUN_HOME/skills`,
`<workdir>/.agents/skills` and `~/.agents/skills`, and exposed as the `/skills`
command plus the `list_skills` / `activate_skill` / `read_skill_file` /
`run_skill_script` tools. A bundle dropped in after startup is picked up
automatically on the next message.
"""
from typing import Any

from xun import Command, ExtensionContext, ToolCallContext
from xun.display_abstract import ShowExtensionsEvent

from .catalog import ensure_fresh, require, roots, session
from .tools import activate_skill


def _discovery_hint(agent: Any) -> str:
    """Where to drop a bundle so the auto resync finds it."""
    roots_hint = "\n".join(f"  {root}  ({source})" for root, source in roots(agent))
    return ("No skills discovered. Add a directory holding a `SKILL.md` whose frontmatter "
            f"sets `name` and `description` under one of:\n{roots_hint}\nThey are picked up automatically on the next message.")


def setup_extension(ctx: ExtensionContext) -> None:
    agent = ctx.agent
    ensure_fresh(agent)
    agent.hooks.before_execution.add(lambda args: ensure_fresh(args.agent))

    def command(agent: Any, args: list[str]) -> None:
        """List, inspect, or activate discovered skills.

        With no arguments, list each discovered skill with its source and current
        activation status, or name the roots that were searched when there are none.
        `info` shows one skill's metadata and bundled files without activating it.
        `activate` loads its instructions and refreshes the system prompt catalog.

        Usage:
            /skills                  # list discovered skills
            /skills info <name>      # inspect <name> without activating it
            /skills activate <name>  # activate <name> and show its instructions
        """
        available, issues = ensure_fresh(agent)
        if not args:
            if not available:
                agent.info(_discovery_hint(agent))
            if available or issues:
                records = [type("Info", (), {"name": s.name, "description": s.description, "status": "loaded" if s.name in session(agent).active else "ready", "reason": None, "source": s.source}) for s in available]
                records += [type("Info", (), {"name": name, "description": "", "status": "failed", "reason": reason, "source": None}) for name, reason in issues]
                agent.display_event(ShowExtensionsEvent.from_infos(records, title="Skills"))
            return
        if len(args) != 2 or args[0] not in {"activate", "info"}:
            raise ValueError("Usage: skills [info <name> | activate <name>]")
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

    agent.command.register(Command("skills", command, "List, inspect, or activate skills."))
