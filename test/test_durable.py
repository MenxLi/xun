import itertools
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from xun import Agent, NullDisplay
from xun.session import AgentStore, DurableSession
from xun.session.durable import SessionMeta

BARE = lambda events, stores=[]: (NullDisplay(), [])  # noqa: E731  # fresh or saved: same bare display, no agents

def make_factory(built: list[Agent]):
    """Fresh or saved factory: one agent per store, or one fresh when none are stored."""
    def factory(events, stores: list[AgentStore] = []):
        display = NullDisplay()
        agents: list[Agent] = []
        for store in stores or [None]:
            agent = Agent(display=display)
            if store is not None:
                store.load(agent)
            built.append(agent)
            agents.append(agent)
        return display, agents
    return factory


def attach(session: DurableSession, **kwargs) -> "Agent[Agent.T.Init]":
    return session._attach(Agent(display=session.display, **kwargs))  # agents are built on the session's display


class DurableSessionTest(unittest.TestCase):
    def test_attach_layout_run_end_and_close_saves(self) -> None:
        with TemporaryDirectory() as home:
            session = DurableSession.new("s1", home_dir=Path(home), factory=BARE)
            agent = attach(session, name="worker")  # attach initializes and binds the display
            slot = session.directory / "agents" / agent.identifier
            self.assertEqual(agent.workspace.workdir, session.directory / "workspace")
            self.assertTrue(agent.workspace.workdir.is_dir())
            self.assertEqual(agent.workspace.tempdir.external_path, session.directory / "tempdir")

            agent.conversation.add_user_message("in run")
            with agent.run_scope():
                pass  # run_end fires the save
            self.assertIn("in run", (slot / "conversation.json").read_text())

            agent.conversation.add_user_message("after run")
            session.close()  # close finalizes the agent itself
            self.assertTrue(Agent.is_finalized(agent))
            self.assertIn("after run", (slot / "conversation.json").read_text())
            session.close()  # idempotent

    def test_a_killed_session_is_discovered_and_restored(self) -> None:
        """`xunx upgrade` force-removes the container, so `close()` never runs: meta
        and snapshots ride on attach and on every save instead, renaming included."""
        with TemporaryDirectory() as home:
            DurableSession.new("bare", home_dir=Path(home), factory=BARE)  # never had an agent: hidden
            session = DurableSession.new("ran", name="Killed", home_dir=Path(home), factory=BARE)
            agent = attach(session, name="survivor")
            self.assertTrue((session.directory / "session.json").is_file())  # registered by its first agent
            agent.conversation.add_user_message("remember me")
            session.name = "Renamed"
            with agent.run_scope():
                pass  # the run-end save carries message and new name; close() is not needed
            identifier = agent.identifier

            rebuilt: list[Agent] = []
            found = DurableSession.discover(factory=make_factory(rebuilt), home_dir=Path(home))
            self.assertEqual([(m.session_id, m.name) for m in found], [("ran", "Renamed")])
            self.assertEqual(len(rebuilt), 1)  # stored agent: exactly one, no fresh extra

            restored = rebuilt[0]  # the session initialized the factory agent
            self.assertEqual((restored.name, restored.identifier), ("survivor", identifier))
            param = cast(dict, restored.conversation.messages[-1].completion_param())
            self.assertEqual(param["content"], "remember me")
            self.assertEqual(restored.workspace.workdir, found[0].directory / "workspace")

            found[0].destroy()
            self.assertEqual(
                DurableSession.discover(factory=BARE, home_dir=Path(home)), [])

    def test_save_meta_flushes_a_rename_without_a_run(self) -> None:
        with TemporaryDirectory() as home:
            session = DurableSession.new("s1", name="Before", home_dir=Path(home), factory=BARE)
            session.name = "After"
            session.save_meta()
            self.assertEqual(SessionMeta.load(session.directory, fallback_name="s1").name, "After")
            session.close()
            session.name = "Too late"
            session.save_meta()  # an ended session never writes
            self.assertEqual(SessionMeta.load(session.directory, fallback_name="s1").name, "After")

    def test_destroy_releases_agents_and_erases_directory(self) -> None:
        with TemporaryDirectory() as home:
            session = DurableSession.new("s1", home_dir=Path(home), factory=BARE)
            self.assertEqual(session.mount_path, "/s1")
            agent = attach(session)
            agent.conversation.add_user_message("doomed")
            session.destroy()
            self.assertTrue(Agent.is_finalized(agent))
            self.assertFalse(session.directory.exists())
            session.close()  # a closed session stays erased
            self.assertFalse(session.directory.exists())
            with self.assertRaisesRegex(RuntimeError, "already ended"):
                attach(session)

    def test_close_skips_display_guests(self) -> None:
        """Only members are saved and finalized; a guest sharing the display
        (like an inherited sub-agent) is its owner's to release."""
        with TemporaryDirectory() as home:
            session = DurableSession.new("s1", home_dir=Path(home), factory=BARE)
            member = attach(session, name="member")
            guest = Agent(name="guest", display=session.display).initialize()
            guest.conversation.add_user_message("guest note")
            session.close()
            self.assertTrue(Agent.is_finalized(member))
            self.assertFalse(Agent.is_finalized(guest))
            self.assertFalse((session.directory / "agents" / guest.identifier).exists())
            session.destroy()

    def test_sessions_and_stores_come_back_in_creation_order(self) -> None:
        """Reload order must come from the born-once created_at, never from random slot names."""
        agent_ids = ["zz", "aa", "mm"]  # sorts opposite to creation order
        clock = itertools.count(1)  # strictly increasing: birth order unambiguous
        with TemporaryDirectory() as home, patch("time.time", side_effect=lambda: next(clock)):
            for i, ids in enumerate((agent_ids, ["p1", "p2"])):
                session = DurableSession.new(f"s{i}", home_dir=Path(home), factory=BARE)
                for identifier in ids:
                    agent = attach(session, identifier=identifier)
                    with agent.run_scope():
                        pass  # the first save stamps the birth
                session.close()

            rebuilt: list[Agent] = []
            found = DurableSession.discover(factory=make_factory(rebuilt), home_dir=Path(home))
            self.assertEqual([m.session_id for m in found], ["s0", "s1"])
            self.assertEqual([a.identifier for a in rebuilt], agent_ids + ["p1", "p2"])
            for m in found:
                m.destroy()

    def test_factory_built_agents_survive_a_kill(self) -> None:
        with TemporaryDirectory() as home:
            built: list[Agent] = []
            session = DurableSession.new("s1", home_dir=Path(home), factory=make_factory(built))
            self.assertEqual(len(built), 1)  # born with exactly one fresh agent
            self.assertTrue(built[0].workspace.workdir.is_dir())  # adopted: session workspace in place
            # killed before any save: session.json exists (attach wrote it) but no snapshots -> fresh again
            back = DurableSession.from_saved("s1", home_dir=Path(home), factory=make_factory(built))
            self.assertEqual(len(built), 2)
            session.destroy()
            back.destroy()

    def test_new_creates_only_from_saved_restores(self) -> None:
        with TemporaryDirectory() as home:
            with self.assertRaisesRegex(FileNotFoundError, "No such session"):
                DurableSession.from_saved("ghost", home_dir=Path(home), factory=BARE)
            session = DurableSession.new("s1", name="Live", home_dir=Path(home), factory=BARE)
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                DurableSession.new("s1", home_dir=Path(home), factory=BARE)
            session.close()
            reopened = DurableSession.from_saved("s1", home_dir=Path(home), factory=BARE)
            self.assertEqual(reopened.name, "Live")
            reopened.destroy()


if __name__ == "__main__":
    unittest.main()
