from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

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
class Session:
    active: set[str] = field(default_factory=set)
    granted_scripts: set[str] = field(default_factory=set)


def session(agent: Any) -> Session:
    return agent.state.setdefault("skills-extension", Session())


def _roots(agent: Any) -> list[tuple[Path, str]]:
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


def skills(agent: Any) -> tuple[list[Skill], list[tuple[str, str]]]:
    discovered: list[Skill] = []
    issues: list[tuple[str, str]] = []
    names: set[str] = set()
    for root, source in _roots(agent):
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


def require(agent: Any, name: str) -> Skill:
    discovered, _ = skills(agent)
    for skill in discovered:
        if skill.name == name:
            return skill
    available = ", ".join(skill.name for skill in discovered) or "none"
    raise ValueError(f"Unknown skill '{name}'. Available: {available}")


def refresh(agent: Any) -> None:
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