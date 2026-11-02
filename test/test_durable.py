import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from xun import Agent, NullDisplay
from xun.session import DurableSession

DISPLAY = lambda store: NullDisplay()  # noqa: E731


class DurableSessionTest(unittest.TestCase):
    def test_attach_layout_run_end_and_close_saves(self) -> None:
        with TemporaryDirectory() as home:
            session = DurableSession("s1", home_dir=Path(home), display_factory=DISPLAY)
            agent = Agent(name="worker", display=NullDisplay())
            session.attach(agent)
            slot = session.directory / agent.identifier
            self.assertTrue((slot / "workspace").is_dir())
            self.assertTrue((slot / "tempdir").is_dir())
            self.assertEqual(agent.workspace.workdir, slot / "workspace")
            self.assertIsNotNone(agent.workspace.tempdir.external_path)

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

    def test_destroy_releases_agents_and_erases_directory(self) -> None:
        with TemporaryDirectory() as home:
            session = DurableSession("s1", home_dir=Path(home), display_factory=DISPLAY)
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

    def test_discover_restores_names_and_restore_round_trip(self) -> None:
        with TemporaryDirectory() as home:
            DurableSession("orphan", home_dir=Path(home), display_factory=DISPLAY)  # never closed: no session.json
            session = DurableSession("alpha", name="Research", home_dir=Path(home), display_factory=DISPLAY)
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
            self.assertEqual(restored.workspace.workdir, session2.directory / identifier / "workspace")

            session2.destroy()
            self.assertEqual(
                DurableSession.discover(display_factory=DISPLAY, home_dir=Path(home)), [])


if __name__ == "__main__":
    unittest.main()
