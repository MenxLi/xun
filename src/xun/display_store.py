"""Event ring buffer and standalone-HTML rendering, shared by every display."""

from __future__ import annotations
from typing import Any, Optional, Sequence, Literal, Self
from collections import deque
import json
import threading
import time
from datetime import datetime
from dataclasses import dataclass, field, replace
import jinja2
import markdown
from markupsafe import Markup, escape
from .config import ASSET_DIR
from .display_event import (
    AgentBindEvent,
    AgentUnbindEvent,
    ConfirmEvent,
    DisplayEvent,
    ErrorEvent,
    HTMLInfoEvent,
    InfoEvent,
    ModelMessageEvent,
    ModelWorkingEvent,
    ShowExtensionsEvent,
    ShowHelpEvent,
    ShowHistoryEvent,
    ShowToolsEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserCommandEvent,
    UserMessageEvent,
    WarningEvent,
)


def _event_time(event: DisplayEvent) -> str:
    return datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S")


def _maybe_json(value: Any) -> Any:
    """Parse a JSON-object/array string so tool details render structured."""
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    return parsed if isinstance(parsed, (dict, list)) else value


def _render_markdown(text: str) -> Markup:
    return Markup(markdown.markdown(str(escape(text)), extensions=["fenced_code", "tables"]))


@dataclass(frozen=True)
class RenderToolRow:
    name: str
    args: Any
    result: Any = None
    time: str = ""


@dataclass(frozen=True)
class RenderStep:
    kind: Literal["reason", "tools", "confirm"]
    html: Markup | None = None
    tools: tuple[RenderToolRow, ...] = ()
    prompt: str = ""
    choices: tuple[str, ...] = ()
    choice: str = ""
    message: Optional[str] = None
    time: str = ""
    source: str = "user"


@dataclass
class _TurnAccum:
    name: str
    time: str
    tool_calls: list[RenderToolRow] = field(default_factory=list)
    steps: list[RenderStep] = field(default_factory=list)
    approvals: int = 0
    tokens: Optional[int] = None

    def add_tool_call(self, row: RenderToolRow) -> None:
        last = self.steps[-1] if self.steps else None
        if last is not None and last.kind == "tools":
            self.steps[-1] = replace(last, tools=(*last.tools, row))
        else:
            self.steps.append(RenderStep(kind="tools", tools=(row,)))

    def set_tool_result(self, row: RenderToolRow, result: Any) -> None:
        updated = replace(row, result=result)
        self.tool_calls[next(i for i, r in enumerate(self.tool_calls) if r is row)] = updated
        for i, step in enumerate(self.steps):
            if step.kind == "tools" and any(r is row for r in step.tools):
                self.steps[i] = replace(step, tools=tuple(updated if r is row else r for r in step.tools))
                break


@dataclass(frozen=True)
class TurnBlock:
    """One agent's activity within a batch, as the web UI's per-agent turn."""
    name: str
    steps: tuple[RenderStep, ...]
    approvals: int
    tokens: Optional[int]
    time: str

    @property
    def details(self) -> int:
        return sum(len(step.tools) if step.kind == "tools" else 1 for step in self.steps)


@dataclass(frozen=True)
class Block:
    """One render block in the standalone HTML stream; `kind` selects the template branch."""
    kind: Literal["user", "message", "command", "notice", "lifecycle", "html_info", "help", "tools", "extensions", "history", "batch"]
    time: str
    content: Markup | None = None
    html: Any = None
    images: tuple[str, ...] = ()
    label: str = ""
    tokens: Optional[int] = None
    name: str = ""
    arguments: Optional[str] = None
    level: str = ""
    title: Optional[str] = None
    commands: tuple[Any, ...] = ()
    tools: tuple[Any, ...] = ()
    extensions: tuple[Any, ...] = ()
    history: tuple[Any, ...] = ()
    agents: tuple[TurnBlock, ...] = ()

    @property
    def single(self) -> bool:
        return len(self.agents) == 1

    @property
    def details(self) -> int:
        return sum(agent.details for agent in self.agents)

    @property
    def approvals(self) -> int:
        return sum(agent.approvals for agent in self.agents)


def _group_events(events: Sequence[DisplayEvent]) -> list[Block]:
    """Group a display event stream into render blocks, mirroring the web UI's
    EventStream: user input breaks the stream, agent activity (tool calls,
    reasoning, auto-approvals) collapses into batches, everything else stands alone.
    """
    blocks: list[Block] = []
    batch_agents: list[_TurnAccum] | None = None
    batch_time = ""
    turns: dict[str, _TurnAccum] = {}
    tool_rows: dict[str, tuple[_TurnAccum, RenderToolRow]] = {}

    def close_batch() -> None:
        nonlocal batch_agents
        if batch_agents is not None:
            agents = tuple(
                TurnBlock(a.name, tuple(a.steps), a.approvals, a.tokens, a.time)
                for a in batch_agents if a.steps or a.approvals
            )
            if agents:
                blocks.append(Block(kind="batch", time=batch_time, agents=agents))
        batch_agents = None
        turns.clear()

    def open_activity(event: DisplayEvent) -> _TurnAccum:
        nonlocal batch_agents, batch_time
        stamp = _event_time(event)
        if batch_agents is None:
            batch_agents = []
            turns.clear()
        batch_time = stamp
        activity = turns.get(event.agent.identifier)
        if activity is None:
            activity = _TurnAccum(name=event.agent.name, time=stamp)
            turns[event.agent.identifier] = activity
            batch_agents.append(activity)
        activity.time = stamp
        return activity

    def add_notice(event: DisplayEvent, level: str, message: str) -> None:
        close_batch()
        blocks.append(Block(kind="notice", level=level, html=_render_markdown(message), time=_event_time(event)))

    for event in events:
        payload = event.payload
        stamp = _event_time(event)
        match payload:
            case UserMessageEvent() as p:
                close_batch()
                blocks.append(Block(kind="user", content=_render_markdown(p.content),
                                    images=tuple(image.value for image in p.images), time=stamp))
            case UserCommandEvent() as p:
                close_batch()
                blocks.append(Block(kind="command", name=p.name, arguments=p.arguments, time=stamp))
            case ToolResultEvent() as p:
                target = tool_rows.get(p.tool_call_id)
                if target is not None:
                    target[0].set_tool_result(target[1], _maybe_json(p.result))
            case ToolCallEvent() as p:
                activity = open_activity(event)
                row = RenderToolRow(name=p.tool_name, args=_maybe_json(p.args), time=stamp)
                if p.tool_call_id:
                    tool_rows[p.tool_call_id] = (activity, row)
                activity.tool_calls.append(row)
                activity.add_tool_call(row)
            case ModelMessageEvent() as p:
                if p.reasoning and p.reasoning.strip():
                    open_activity(event).steps.append(RenderStep(kind="reason", html=_render_markdown(p.reasoning)))
                if p.content.strip():
                    close_batch()
                    blocks.append(Block(kind="message", label=event.agent.name,
                                        html=_render_markdown(p.content), tokens=p.total_tokens, time=stamp))
                else:
                    open_activity(event).tokens = p.total_tokens
            case ModelWorkingEvent():
                open_activity(event)
            case AgentBindEvent():
                close_batch()
                blocks.append(Block(kind="lifecycle", level="bound", name=event.agent.name, time=stamp))
            case AgentUnbindEvent():
                close_batch()
                blocks.append(Block(kind="lifecycle", level="unbound", name=event.agent.name, time=stamp))
            case ConfirmEvent() as p:
                # Auto-confirms keep the batch counter and still render as a pill, matching the web UI.
                activity = open_activity(event)
                if p.source == "auto":
                    activity.approvals += 1
                activity.steps.append(RenderStep(kind="confirm", prompt=p.prompt, choices=tuple(p.choices),
                                                 choice=p.choice, message=p.message, time=stamp, source=p.source))
            case HTMLInfoEvent() as p:
                close_batch()
                blocks.append(Block(kind="html_info", title=p.title, html=p.html, time=stamp))
            case InfoEvent() as p:
                if p.message.startswith("[user] "):
                    close_batch()
                    blocks.append(Block(kind="user", content=_render_markdown(p.message[len("[user] "):]),
                                        time=stamp))
                else:
                    add_notice(event, "info", p.message)
            case WarningEvent() as p:
                add_notice(event, "warning", p.message)
            case ErrorEvent() as p:
                add_notice(event, "error", p.message)
            case ShowHelpEvent() as p:
                close_batch()
                blocks.append(Block(kind="help", commands=tuple(c.model_dump() for c in p.commands), time=stamp))
            case ShowToolsEvent() as p:
                close_batch()
                blocks.append(Block(kind="tools", tools=tuple(t.model_dump() for t in p.tools), time=stamp))
            case ShowExtensionsEvent() as p:
                close_batch()
                blocks.append(Block(kind="extensions", title=p.title, extensions=tuple(e.model_dump() for e in p.extensions), time=stamp))
            case ShowHistoryEvent() as p:
                close_batch()
                blocks.append(Block(kind="history", history=tuple(dict(r) for r in p.history), time=stamp))
            case _:
                pass  # running lifecycle events are not rendered

    close_batch()
    return blocks


class DisplayEventStoreMixin:
    """Event ring buffer and standalone-HTML rendering. """

    DEFAULT_EVENT_BUFFER_SIZE = 0
    """Zero on purpose: payloads can be large (tool results, base64 images),
    consumers need to set their own capacity via `with_buffer_size`."""

    _event_ring: deque[DisplayEvent]
    _event_ring_lock: threading.Lock

    def _event_store(self) -> tuple[deque[DisplayEvent], threading.Lock]:
        """Ring buffer of every event seen by this display, plus its lock."""
        if not hasattr(self, "_event_ring_lock"):
            self._event_ring_lock = threading.Lock()
        if not hasattr(self, "_event_ring"):
            self._event_ring = deque(maxlen=self.DEFAULT_EVENT_BUFFER_SIZE)
        return self._event_ring, self._event_ring_lock

    def with_buffer_size(self, max_events: int) -> Self:
        """Set how many events the ring buffer retains (oldest dropped beyond that)."""
        ring, lock = self._event_store()
        with lock:
            self._event_ring = deque(ring, maxlen=max_events)
        return self

    def _record_event(self, event: DisplayEvent) -> None:
        """Store an event for later rendering. `display_event` calls this for every event."""
        buffer, lock = self._event_store()
        with lock:
            buffer.append(event)

    def events(self, agent_id: str | None = None) -> list[DisplayEvent]:
        """Buffered events, oldest first; optionally only those emitted by one agent."""
        buffer, lock = self._event_store()
        with lock:
            events = list(buffer)
        return [event for event in events if event.agent.identifier == agent_id] if agent_id else events

    def clear_events(self, agent_id: str | None = None) -> None:
        """Drop buffered events, all of them or only those from one agent."""
        buffer, lock = self._event_store()
        with lock:
            if agent_id is None:
                buffer.clear()
            else:
                kept = [event for event in buffer if event.agent.identifier != agent_id]
                buffer.clear()
                buffer.extend(kept)

    def render_history_as_html(self, title: str = "Session") -> str:
        """Render this display's buffered events as a standalone HTML page."""
        events = self.events()
        latest_tokens = next(
            (event.payload.total_tokens for event in reversed(events) if isinstance(event.payload, ModelMessageEvent)),
            None,
        )
        environment = jinja2.Environment(autoescape=True)
        environment.policies["json.dumps_kwargs"] = {"ensure_ascii": False}
        return environment.from_string((ASSET_DIR / "events.template.html").read_text(encoding="utf-8")).render(
            blocks=_group_events(events),
            meta={
                "title": title,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "total_tokens": latest_tokens,
            },
        )

__all__ = ["DisplayEventStoreMixin"]
