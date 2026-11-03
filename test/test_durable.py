import itertools
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from xun import Agent, NullDisplay
from xun.session import DurableSession

DISPLAY = lambda store: NullDisplay()  # noqa: E731


class DurableSessionTest(unittest.TestCase):
    def test_attach_layout_run_end_and_close_saves(self) -> None:
        with TemporaryDirectory() as home:
            session = DurableSession.new("s1", home_dir=Path(home), display_factory=DISPLAY)
            agent = Agent(name="worker", display=NullDisplay())
            session.attach(agent)
            slot = session.directory / "agents" / agent.identifier
            self.assertEqual(agent.workspace.workdir, session.directory / "workspace")
            self.assertTrue(agent.workspace.workdir.is_dir())
            self.assertEqual(agent.workspace.tempdir.external_path, session.directory / "tempdir")

            agent = agent.initialize()
            agent.conversation.add_user_message("in run")
            with agent.run_scope():
                pass  # run_end fires the save
            self.assertIn("in run", (slot / "conversation.json").read_text())

            agent.conversation.add_user_message("after run")
            session.close()  # close finalizes the agent itself
            self.assertTrue(Agent.is_finalized(agent))
            self.assertIn("after run", (slot / "conversation.json").read_text())
            session.close()  # idempotent

    def test_attach_registers_the_session_so_a_kill_cannot_hide_it(self) -> None:
        """`xunx upgrade` force-removes the container, so `close()` never runs: the
        meta rides on attach and on every save instead, renaming included."""
        with TemporaryDirectory() as home:
            session = DurableSession.new("ran", name="Killed", home_dir=Path(home), display_factory=DISPLAY)
            agent = Agent(name="survivor", display=NullDisplay())
            session.attach(agent)
            self.assertTrue((session.directory / "session.json").is_file())  # registered by its first agent
            agent = agent.initialize()
            session.name = "Renamed"
            with agent.run_scope():
                pass  # the run-end save carries the new name; close() is not needed
            DurableSession.new("bare", home_dir=Path(home), display_factory=DISPLAY)  # never had an agent: hidden

            found = DurableSession.discover(display_factory=DISPLAY, home_dir=Path(home))
            self.assertEqual([(m.session_id, m.name) for m in found], [("ran", "Renamed")])
            self.assertIn("survivor", (found[0].directory / "agents" / agent.identifier / "meta.json").read_text())

    def test_destroy_releases_agents_and_erases_directory(self) -> None:
        with TemporaryDirectory() as home:
            session = DurableSession.new("s1", home_dir=Path(home), display_factory=DISPLAY)
            self.assertEqual(session.mount_path, "/s1")
            agent = Agent(display=NullDisplay())
            session.attach(agent)
            agent = agent.initialize()
            agent.conversation.add_user_message("doomed")
            session.destroy()
            self.assertTrue(Agent.is_finalized(agent))
            self.assertFalse(session.directory.exists())
            session.close()  # a closed session stays erased
            self.assertFalse(session.directory.exists())
            with self.assertRaisesRegex(RuntimeError, "already ended"):
                session.attach(Agent(display=NullDisplay()))

    def test_entries_and_sessions_come_back_in_creation_order(self) -> None:
        """Reload order must come from the born-once created_at, never from random slot names."""
        agent_ids = ["zz", "aa", "mm"]  # sorts opposite to creation order
        clock = itertools.count(1)  # strictly increasing: birth order unambiguous
        with TemporaryDirectory() as home, patch("time.time", side_effect=lambda: next(clock)):
            for i, ids in enumerate((agent_ids, ["p1", "p2"])):
                session = DurableSession.new(f"s{i}", home_dir=Path(home), display_factory=DISPLAY)
                for identifier in ids:
                    agent = Agent(identifier=identifier, display=NullDisplay())
                    session.attach(agent)
                    agent = agent.initialize()
                    with agent.run_scope():
                        pass  # the first save stamps the birth
                session.close()

            found = DurableSession.discover(display_factory=DISPLAY, home_dir=Path(home))
            self.assertEqual([m.session_id for m in found], ["s0", "s1"])
            self.assertEqual(list(found[0].entries()), agent_ids)
            self.assertEqual(list(found[1].entries()), ["p1", "p2"])
            for m in found:
                m.destroy()

    def test_new_creates_only_from_saved_restores(self) -> None:
        with TemporaryDirectory() as home:
            with self.assertRaisesRegex(FileNotFoundError, "No such session"):
                DurableSession.from_saved("ghost", home_dir=Path(home), display_factory=DISPLAY)
            session = DurableSession.new("s1", name="Live", home_dir=Path(home), display_factory=DISPLAY)
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                DurableSession.new("s1", home_dir=Path(home), display_factory=DISPLAY)
            session.close()
            reopened = DurableSession.from_saved("s1", home_dir=Path(home), display_factory=DISPLAY)
            self.assertEqual(reopened.name, "Live")
            reopened.destroy()

    def test_discover_restores_names_and_restore_round_trip(self) -> None:
        with TemporaryDirectory() as home:
            DurableSession.new("orphan", home_dir=Path(home), display_factory=DISPLAY)  # no agent attached: no session.json
            session = DurableSession.new("alpha", name="Research", home_dir=Path(home), display_factory=DISPLAY)
            agent = Agent(name="restorable", display=NullDisplay())
            session.attach(agent)
            agent = agent.initialize()
            agent.conversation.add_user_message("remember me")
            identifier = agent.identifier
            session.close()

            found = DurableSession.discover(display_factory=DISPLAY, home_dir=Path(home))
            self.assertEqual([(m.session_id, m.name) for m in found], [("alpha", "Research")])
            entries = found[0].entries()
            self.assertEqual(list(entries), [identifier])

            session2 = found[0]
            restored = Agent(display=NullDisplay())
            entries[identifier].load(restored)
            session2.attach(restored)
            restored = restored.initialize()
            self.assertEqual(restored.name, "restorable")
            self.assertEqual(restored.identifier, identifier)
            param = cast(dict, restored.conversation.messages[-1].completion_param())
            self.assertEqual(param["content"], "remember me")
            self.assertEqual(restored.workspace.workdir, session2.directory / "workspace")

            session2.destroy()
            self.assertEqual(
                DurableSession.discover(display_factory=DISPLAY, home_dir=Path(home)), [])


if __name__ == "__main__":
    unittest.main()
