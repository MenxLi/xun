from __future__ import annotations

import json
import shlex
import shutil
import sys

from xun import ToolCallContext, tool_attr
from xun.tools.cmd import CmdExecResult, run_command, truncate_output

from .catalog import Skill, refresh, require, session, skills

SCRIPT_INTERPRETERS = {
    ".py": lambda: sys.executable,
    ".sh": lambda: shutil.which("bash"),
    ".js": lambda: shutil.which("node"),
}


@tool_attr(name="list_skills")
def list_skills(ctx: ToolCallContext) -> str:
    """List available skills. Use activate_skill to load one skill's instructions."""
    discovered, _ = skills(ctx.agent)
    return json.dumps([{"name": skill.name, "description": skill.description, "source": skill.source} for skill in discovered], ensure_ascii=False)


@tool_attr(name="activate_skill")
def activate_skill(ctx: ToolCallContext, name: str) -> str:
    """Load a skill's instructions before performing a task that matches its description."""
    skill = require(ctx.agent, name)
    session(ctx.agent).active.add(name)
    refresh(ctx.agent)
    files = skill.files()
    footer = [f"Skill directory: {skill.directory}"]
    if files:
        footer += ["Bundled files:", *(f"  {file}" for file in files[:60])]
    return f'<skill_content name="{skill.name}" source="{skill.source}">\n{skill.body}\n\n' + "\n".join(footer) + "\n</skill_content>"


def _active_skill(ctx: ToolCallContext, name: str) -> Skill:
    if name not in session(ctx.agent).active:
        raise ValueError(f"Skill '{name}' is not activated. Call activate_skill first.")
    return require(ctx.agent, name)


@tool_attr(name="read_skill_file")
def read_skill_file(ctx: ToolCallContext, skill: str, file: str = "") -> str:
    """Read a text file bundled with an activated skill, addressed relative to its directory."""
    selected = _active_skill(ctx, skill)
    if not file:
        return json.dumps(selected.files(), ensure_ascii=False)
    path = selected.resolve(file)
    if not path.is_file():
        raise ValueError(f"File `{file}` not found in skill '{skill}'.")
    return path.read_text(encoding="utf-8", errors="replace")


@tool_attr(name="run_skill_script")
def run_skill_script(
    ctx: ToolCallContext,
    skill: str,
    script: str,
    args: list[str] | None = None,
    timeout: float = 300,
    max_output_size: int | None = 16_000,
) -> CmdExecResult:
    """Run an activated skill's bundled .py, .sh or .js script without shell semantics."""
    selected = _active_skill(ctx, skill)
    target = selected.resolve(script)
    interpreter = SCRIPT_INTERPRETERS.get(target.suffix)
    if interpreter is None or (executable := interpreter()) is None or not target.is_file():
        raise ValueError(f"Unsupported or missing skill script '{script}'.")
    grants = session(ctx.agent).granted_scripts
    command = shlex.join([executable, str(target), *(args or [])])
    if str(target) not in grants:
        answer = ctx.agent.get_choice(
            "Allow this skill script to run?",
            ["Allow once", "Allow for this session", "Deny"],
            message=f"Skill: {skill}\nScript: {script}\nCommand: {command}",
            title="Skill Script Execution",
            default="Allow once",
        )
        if answer.choice == "Deny":
            raise RuntimeError(f"Execution of `{script}` was denied.")
        if answer.choice == "Allow for this session" and answer.source == "user":
            grants.add(str(target))
    result = run_command([executable, str(target), *(args or [])], timeout, ctx.agent.workspace.workdir, None, ctx.agent.cancel_event.is_set)
    return CmdExecResult(args=command, stdout=truncate_output(result.stdout.strip(), max_output_size), stderr=truncate_output(result.stderr.strip(), max_output_size), returncode=result.returncode)