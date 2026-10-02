import os
from typing import Any

from xun import Command, ExtensionContext, ToolCallContext
from xun.display_abstract import ShowExtensionsEvent
from .catalog import refresh, require, session, skills
from .tools import activate_skill, list_skills, read_skill_file, run_skill_script


def setup_extension(ctx: ExtensionContext) -> None:
    agent = ctx.agent
    available, _ = skills(agent)
    if not available:
        return
    agent.toolbox.register(list_skills, activate_skill, read_skill_file)
    if os.name != "nt":
        agent.toolbox.register(run_skill_script)
    refresh(agent)

    def command(agent: Any, args: list[str]) -> None:
        """List, inspect, activate, or reload discovered skills.

        With no arguments, show each discovered skill, its source and current
        activation status. `info` shows one skill's metadata and bundled files
        without activating it. `activate` loads its instructions and refreshes
        the system prompt catalog. `reload` rescans all skill roots.

        Usage:
            /skill                  # list discovered skills
            /skill info <name>      # inspect <name> without activating it
            /skill activate <name>  # activate <name> and show its instructions
            /skill reload           # rescan skill directories
        """
        if not args:
            available, issues = skills(agent)
            records = [type("Info", (), {"name": s.name, "description": s.description, "status": "loaded" if s.name in session(agent).active else "ready", "reason": None, "source": s.source}) for s in available]
            records += [type("Info", (), {"name": name, "description": "", "status": "failed", "reason": reason, "source": None}) for name, reason in issues]
            agent.display_event(ShowExtensionsEvent.from_infos(records, title="Skills"))
            return
        if args == ["reload"]:
            refresh(agent)
            agent.info("Skills reloaded.")
            return
        if len(args) != 2 or args[0] not in {"activate", "info"}:
            raise ValueError("Usage: skill [info <name> | activate <name> | reload]")
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

    agent.command.register(Command("skill", command, "List, inspect, activate, or reload skills."))