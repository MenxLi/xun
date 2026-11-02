import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from xun import Agent, NullDisplay
from xun.session import SessionManager


class SessionManagerTest(unittest.TestCase):
    def test_attach_layout_run_end_and_exit_saves(self) -> None:
        with TemporaryDirectory() as home:
            manager = SessionManager("s1", home_dir=Path(home))
            with manager:
                agent = Agent(name="worker", display=NullDisplay())
                manager.attach(agent)
                slot = manager.directory / agent.identifier
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
            self.assertIn("after run", (slot / "conversation.json").read_text())  # exit saved

    def test_event_store_is_a_closed_singleton(self) -> None:
        with TemporaryDirectory() as home:
            manager = SessionManager("s1", home_dir=Path(home))
            with manager:
                store = manager.event_store()
                self.assertIs(manager.event_store(), store)
            self.assertTrue((manager.directory / "events.db").exists())

    def test_discover_and_restore_round_trip(self) -> None:
        with TemporaryDirectory() as home:
            with SessionManager("alpha", home_dir=Path(home)) as manager:
                agent = Agent(name="restorable", display=NullDisplay())
                manager.attach(agent)
                agent = agent.initialize()
                agent.conversation.add_user_message("remember me")
                identifier = agent.identifier

            found = SessionManager.discover(home_dir=Path(home))
            self.assertEqual([m.session_id for m in found], ["alpha"])
            entries = found[0].entries()
            self.assertEqual(list(entries), [identifier])

            with found[0] as manager2:
                restored = Agent(display=NullDisplay())
                entries[identifier].load(restored)
                manager2.attach(restored)
                restored = restored.initialize()
                self.assertEqual(restored.name, "restorable")
                self.assertEqual(restored.identifier, identifier)
                param = cast(dict, restored.conversation.messages[-1].completion_param())
                self.assertEqual(param["content"], "remember me")
                self.assertEqual(restored.workspace.workdir, manager2.directory / identifier / "workspace")


if __name__ == "__main__":
    unittest.main()
