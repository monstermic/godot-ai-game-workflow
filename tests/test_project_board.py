from __future__ import annotations

import unittest

from aigame.project import configure_project, project_plan


class FakeProjectClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def ensure_project(self, title: str, repository: str) -> int:
        self.calls.append(("ensure_project", {"title": title, "repository": repository}))
        return 7

    def ensure_fields(self, number: int, fields: dict) -> None:
        self.calls.append(("ensure_fields", {"number": number, "fields": fields}))


class ProjectBoardTests(unittest.TestCase):
    def test_project_plan_contains_statuses_and_operational_fields(self) -> None:
        plan = project_plan("monstermic/godot-ai-game-workflow")
        self.assertEqual(plan["owner"], "monstermic")
        self.assertEqual(plan["statuses"][0], "Intake")
        self.assertEqual(plan["statuses"][-1], "Done")
        self.assertEqual(plan["fields"]["Work ID"]["type"], "TEXT")
        self.assertIn("Vertical Slice", plan["fields"]["Phase"]["options"])

    def test_project_configuration_is_dry_run_safe(self) -> None:
        client = FakeProjectClient()
        preview = configure_project(
            "monstermic/godot-ai-game-workflow", client=client, apply=False
        )
        self.assertEqual(preview["status"], "dry_run")
        self.assertEqual(client.calls, [])
        result = configure_project(
            "monstermic/godot-ai-game-workflow", client=client, apply=True
        )
        self.assertEqual(result["project_number"], 7)
        self.assertEqual([name for name, _ in client.calls], ["ensure_project", "ensure_fields"])


if __name__ == "__main__":
    unittest.main()
