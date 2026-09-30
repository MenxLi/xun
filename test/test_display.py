import unittest
from pathlib import Path
from unittest.mock import Mock

from xun.display_abstract import (
    AgentInfo,
    AgentRunningEndEvent,
    AgentRunningStartEvent,
    DisplayEvent,
)
from xun.displays.display import Display


class DisplayTest(unittest.TestCase):
    def test_running_events_are_ignored(self) -> None:
        display = Display()
        display._unhandled = Mock()
        agent = AgentInfo(name="Xun", identifier="agent-1", workdir=Path.cwd())

        for payload in (AgentRunningStartEvent(), AgentRunningEndEvent()):
            display.on_event(DisplayEvent(
                name=payload.__class__.__name__,
                agent=agent,
                payload=payload,
            ))

        display._unhandled.assert_not_called()


if __name__ == "__main__":
    unittest.main()