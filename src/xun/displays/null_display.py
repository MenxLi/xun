from ..display_abstract import DisplayAbstract, DisplayEvent

class NullDisplay(DisplayAbstract):
    """A display that swallows all events and cannot prompt the user.

    The default for `Agent`: programmatic agents are silent unless a display
    is explicitly attached; interactive entrypoints (setup_agent / CLI) pass
    a real display.
    """
    def get_choice(self, *args, **kwargs) -> str:
        raise NotImplementedError("NullDisplay does not support get_choice.")

    def on_event(self, event: DisplayEvent):
        pass
