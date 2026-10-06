from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Callable

from xun.agent import Agent
from xun.toolcall import ToolAttr
from xun.state_entry import StateEntry

try:
    import yaml
except ImportError as e:
    raise ImportError("PyYAML is required to use the skills extension: {}".format(e))

SKILL_FILE = "SKILL.md"
SECTION_NAME = "agent-skills"
SKIP_DIRS = {".git", "node_modules", "__pycache__"}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    directory: Path
    source: str

    def resolve(self, relative_path: str) -> Path:
        path = (self.directory / relative_path).resolve()
        if not path.is_relative_to(self.directory):
            raise ValueError(f"`{relative_path}` escapes skill '{self.name}'.")
        return path

    def files(self) -> list[str]:
        return sorted(
            path.relative_to(self.directory).as_posix()
            for path in self.directory.rglob("*")
            if path.is_file() and path.name != SKILL_FILE and not (set(path.parts) & SKIP_DIRS)
        )


@dataclass
class Session(StateEntry):
    active: set[str] = field(default_factory=set)
    granted_scripts: set[str] = field(default_factory=set)
    fingerprint: tuple[tuple[str, str], ...] | None = None
    """The (name, description) pairs last synced into the prompt and toolbox."""


def session(agent: Agent[Agent.T.Alive]) -> Session:
    return agent.get_state_entry("skills-extension", Session)


def roots(agent: Agent[Agent.T.Alive]) -> list[tuple[Path, str]]:
    """Where `skills` looks for bundles, highest precedence first."""
    return [
        (Path(os.environ.get("XUN_HOME", Path.cwd() / ".xun")) / "skills", "user"),
        (agent.workspace.workdir / ".agents" / "skills", "project"),
        (Path.home() / ".agents" / "skills", "user"),
    ]


def _parse(path: Path, source: str) -> Skill:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("missing YAML frontmatter")
    _, frontmatter, body = text.split("---", 2)
    metadata = yaml.safe_load(frontmatter)
    if not isinstance(metadata, dict):
        raise ValueError("frontmatter must be a mapping")
    name = str(metadata.get("name") or path.parent.name).strip()
    description = str(metadata.get("description") or "").strip()
    if not name or not description:
        raise ValueError("name and description are required")
    return Skill(name, description, body.strip(), path.parent.resolve(), source)


def skills(agent: Agent[Agent.T.Alive]) -> tuple[list[Skill], list[tuple[str, str]]]:
    discovered: list[Skill] = []
    issues: list[tuple[str, str]] = []
    names: set[str] = set()
    for root, source in roots(agent):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob(SKILL_FILE)):
            if set(path.parts) & SKIP_DIRS:
                continue
            try:
                skill = _parse(path, source)
            except (OSError, ValueError, yaml.YAMLError) as error:
                issues.append((path.parent.name, str(error)))
                continue
            if skill.name in names:
                issues.append((skill.name, "shadowed by a higher-precedence skill"))
                continue
            names.add(skill.name)
            discovered.append(skill)
    return discovered, issues


def require(agent: Agent[Agent.T.Alive], name: str) -> Skill:
    discovered, _ = skills(agent)
    for skill in discovered:
        if skill.name == name:
            return skill
    available = ", ".join(skill.name for skill in discovered) or "none"
    raise ValueError(f"Unknown skill '{name}'. Available: {available}")


def refresh(agent: Agent[Agent.T.Alive]) -> None:
    # TODO: change to use set_system_persistent_section after v1.4
    discovered, _ = skills(agent)
    if not discovered:
        agent.conversation.set_persistent_section(SECTION_NAME, "")
        return
    entries = [{"name": skill.name, "description": skill.description} for skill in discovered]
    active = sorted(session(agent).active)
    content = [
        "## Agent Skills",
        "Skills are task-specific instruction bundles. Call activate_skill(name) before using one.",
        f"Available skills (JSON): {json.dumps(entries, ensure_ascii=False, separators=(',', ':'))}",
    ]
    if active:
        content.append(f"Active skills: {', '.join(active)}. Call activate_skill again to reread its instructions.")
    agent.conversation.set_persistent_section(SECTION_NAME, "\n".join(content))


def _tool_name(func: Callable) -> str:
    """The name `func` registers under: its `tool_attr` override, else its `__name__`."""
    attr = ToolAttr.extract_from(func)
    return (attr.name if attr and attr.name else None) or func.__name__


def sync_tools(agent: Agent[Agent.T.Alive], available: list[Skill]) -> None:
    """Register the skill tools while the catalog is non-empty.

    Additive and idempotent: present names are left alone, so a tool the user
    disabled stays disabled. Runs on every resync and on a sub-agent's setup replay.
    """
    if not available:
        return
    from .tools import activate_skill, list_skills, read_skill_file, run_skill_script

    funcs = [list_skills, activate_skill, read_skill_file]
    if os.name != "nt":
        funcs.append(run_skill_script)  # POSIX only
    present = {tool.name for tool in agent.toolbox.list_tools(include_disabled=True)}
    agent.toolbox.register(*[f for f in funcs if _tool_name(f) not in present])


def ensure_fresh(agent: Agent[Agent.T.Alive]) -> tuple[list[Skill], list[tuple[str, str]]]:
    """Resync tools and the prompt catalog when the bundles changed on disk.

    Only the name/description fingerprint is compared, so a body edit (read
    live at activation) triggers no resync.
    """
    discovered, issues = skills(agent)
    current = session(agent)
    fingerprint = tuple(sorted((s.name, s.description) for s in discovered))
    if fingerprint != current.fingerprint:
        sync_tools(agent, discovered)
        refresh(agent)
        current.fingerprint = fingerprint
    return discovered, issues