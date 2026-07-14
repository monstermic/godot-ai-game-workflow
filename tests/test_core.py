from __future__ import annotations

import unittest

from aigame.core import (
    HumanRequired,
    InvalidTransition,
    MissingCapability,
    choose_next,
    fingerprint,
    transition,
)


def work_item(
    item_id: str,
    *,
    status: str = "ready",
    release_blocker: bool = False,
    risk: int = 1,
    unblocks: int = 0,
    player_value: int = 1,
    estimate: int = 1,
    required_capabilities: list[str] | None = None,
    unknowns: list[dict[str, str]] | None = None,
) -> dict:
    return {
        "schema_version": "1.0",
        "id": item_id,
        "revision": 1,
        "status": status,
        "release_blocker": release_blocker,
        "risk": risk,
        "unblocks": unblocks,
        "player_value": player_value,
        "estimate": estimate,
        "required_capabilities": required_capabilities or [],
        "unknowns": unknowns or [],
        "dependencies": [],
    }


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable_across_mapping_order(self) -> None:
        self.assertEqual(
            fingerprint({"b": 2, "a": [3, 1]}),
            fingerprint({"a": [3, 1], "b": 2}),
        )


class StateMachineTests(unittest.TestCase):
    def test_ready_item_can_be_claimed(self) -> None:
        item = work_item("WI-0001")
        updated = transition(item, "claimed")
        self.assertEqual(updated["status"], "claimed")
        self.assertEqual(updated["revision"], 2)
        self.assertNotEqual(updated["input_fingerprint"], "")

    def test_invalid_transition_is_rejected(self) -> None:
        with self.assertRaises(InvalidTransition):
            transition(work_item("WI-0001"), "done")

    def test_active_item_can_be_blocked(self) -> None:
        updated = transition(work_item("WI-0001", status="validating"), "blocked")
        self.assertEqual(updated["status"], "blocked")


class SchedulerTests(unittest.TestCase):
    def test_release_blocker_wins_before_other_scores(self) -> None:
        selected = choose_next(
            [
                work_item("WI-0002", risk=5, unblocks=8, player_value=5),
                work_item("WI-0001", release_blocker=True),
            ],
            capabilities=set(),
        )
        self.assertEqual(selected["id"], "WI-0001")

    def test_tie_breaks_by_work_item_id(self) -> None:
        selected = choose_next(
            [work_item("WI-0002"), work_item("WI-0001")], capabilities=set()
        )
        self.assertEqual(selected["id"], "WI-0001")

    def test_red_unknown_requires_human(self) -> None:
        item = work_item(
            "WI-0001",
            unknowns=[{"level": "red", "question": "Authoritative multiplayer?"}],
        )
        with self.assertRaises(HumanRequired):
            choose_next([item], capabilities=set())

    def test_missing_capability_is_reported(self) -> None:
        item = work_item("WI-0001", required_capabilities=["godot"])
        with self.assertRaises(MissingCapability) as context:
            choose_next([item], capabilities={"git"})
        self.assertEqual(context.exception.missing, ["godot"])


if __name__ == "__main__":
    unittest.main()
