from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import choice as prompt_choice


CommandProvider = Callable[[], Iterable[tuple[str, str]]]


class SlashCommandCompleter(Completer):
    def __init__(self, provider: CommandProvider) -> None:
        self.provider = provider

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterator[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/") or text.startswith("\\/") or any(char.isspace() for char in text):
            return
        prefix = text[1:]
        for name, description in sorted(self.provider()):
            if name.startswith(prefix):
                yield Completion(
                    name,
                    start_position=-len(prefix),
                    display=f"/{name}",
                    display_meta=description,
                )


@dataclass
class _PromptTakeover:
    released: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    document: Document = field(default_factory=Document)


class _PromptInterrupted(Exception):
    def __init__(self, takeover: _PromptTakeover):
        self.takeover = takeover


@dataclass
class _ActivePrompt:
    app: Application[str]
    ready: threading.Event
    preemptible: bool


class CliSession:
    def __init__(self) -> None:
        self._input_lock = threading.RLock()
        self._state_changed = threading.Condition()
        self._takeover_waiters = 0
        self._command_provider: CommandProvider = lambda: ()
        self._main_session: PromptSession[str] = PromptSession(
            history=InMemoryHistory(),
            completer=SlashCommandCompleter(lambda: self._command_provider()),
            auto_suggest=AutoSuggestFromHistory(),
            complete_while_typing=True,
            enable_history_search=True,
            reserve_space_for_menu=6,
        )
        self._transient_session: PromptSession[str] = PromptSession()
        self._active_prompt: _ActivePrompt | None = None

    def set_command_provider(self, provider: CommandProvider) -> None:
        self._command_provider = provider

    def input(self, prompt: str = "") -> str:
        document = Document()
        while True:
            try:
                return self._prompt(self._main_session, prompt, document, preemptible=True)
            except _PromptInterrupted as interrupted:
                interrupted.takeover.released.set()
                interrupted.takeover.done.wait()
                document = interrupted.takeover.document

    def transient_input(self, prompt: str = "") -> str:
        return self._prompt(self._transient_session, prompt, Document(), preemptible=False)

    def choose(self, prompt: str, choices: list[str], default: int = 1) -> int:
        if not choices:
            raise ValueError("At least one choice is required")
        if default not in range(1, len(choices) + 1):
            raise ValueError("Default choice is out of range")
        with self._input_lock:
            with patch_stdout(raw=True):
                return prompt_choice(
                    message=ANSI(prompt),
                    options=list(enumerate(choices, start=1)),
                    default=default,
                    bottom_toolbar="Up/Down select | Enter confirm",
                )

    @contextmanager
    def takeover_input(self):
        with self._state_changed:
            self._takeover_waiters += 1
        takeover = None
        try:
            takeover = self._request_takeover()
            if takeover is not None:
                takeover.released.wait()
            with self._input_lock:
                yield
        finally:
            if takeover is not None:
                takeover.done.set()
            with self._state_changed:
                self._takeover_waiters -= 1
                self._state_changed.notify_all()

    def _request_takeover(self) -> _PromptTakeover | None:
        with self._state_changed:
            active = self._active_prompt
            if active is None or not active.preemptible:
                return None
        active.ready.wait()
        with self._state_changed:
            app = active.app
            if self._active_prompt is not active or not app.is_running or app.loop is None:
                return None
            loop = app.loop
            takeover = _PromptTakeover()

            def interrupt() -> None:
                if app.is_running:
                    takeover.document = app.current_buffer.document
                    app.exit(exception=_PromptInterrupted(takeover))
                else:
                    takeover.released.set()

            try:
                loop.call_soon_threadsafe(interrupt)
            except RuntimeError:
                return None
            return takeover

    def _prompt(
        self,
        session: PromptSession[str],
        prompt: str,
        document: Document,
        *,
        preemptible: bool,
    ) -> str:
        active = _ActivePrompt(session.app, threading.Event(), preemptible)
        if preemptible:
            with self._state_changed:
                self._state_changed.wait_for(lambda: not self._takeover_waiters)
                self._input_lock.acquire()
                self._active_prompt = active
        else:
            self._input_lock.acquire()
            with self._state_changed:
                self._active_prompt = active
        try:
            with patch_stdout(raw=True):
                return session.prompt(ANSI(prompt), default=document, pre_run=active.ready.set)
        finally:
            active.ready.set()
            with self._state_changed:
                if self._active_prompt is active:
                    self._active_prompt = None
            self._input_lock.release()