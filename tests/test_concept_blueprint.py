from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aigame.cli import main
from aigame.concept import (
    finalize_concept,
    next_concept_task,
    render_concept,
    revise_concept,
    select_concept_pitch,
    start_concept,
    submit_concept_task,
    validate_concept,
)
from aigame.core import WorkflowError, fingerprint
from aigame.context import build_context
from aigame.generator import create_game
from aigame.validation import validate_project


def approval(request: dict, approval_id: str) -> dict:
    value = {
        "schema_version": "1.0",
        "id": approval_id,
        "revision": 1,
        "status": "approved",
        "created_at": "2026-07-14T00:00:00Z",
        "updated_at": "2026-07-14T00:00:00Z",
        "scope_hash": request["scope_hash"],
        "commit_sha": request["commit_sha"],
        "approver": "Game Owner",
        "decision": request["decision"],
        "approved_at": "2026-07-14T00:00:00Z",
    }
    value["input_fingerprint"] = fingerprint(value)
    return value


def pitch_result() -> dict:
    pitches = []
    for index, title in enumerate(("Ember Circuit", "Ashen Relay", "Cinder Crown"), 1):
        pitches.append(
            {
                "title": title,
                "player_fantasy": "Outsmart a living clockwork dungeon.",
                "unique_identity": f"A distinct time-loop direction {index}.",
                "core_loop": "Explore, fight, choose a reward, improve the hub, and return.",
                "scope_summary": "A small offline 2D action roguelite.",
                "beginning": "The hero wakes beside a broken world clock.",
                "ending": "The final keeper falls and the player chooses whether time restarts.",
                "feasibility": "Fits the configured Godot and content budgets.",
                "risks": ["Combat feel requires an early experiment."],
                "scores": {
                    "identity": 4,
                    "coherence": 4,
                    "core_loop": 4,
                    "scope_feasibility": 5,
                    "player_fit": 4,
                },
            }
        )
    return {
        "intake": {
            "audience": "Players who enjoy compact skill-based action roguelites.",
            "genre_tags": ["action", "roguelite"],
            "facts": ["The requested game is small, offline, and clockwork themed."],
            "assumptions": [{"text": "A run targets roughly twenty minutes.", "risk": "green"}],
            "contradictions": [],
            "non_goals": ["online multiplayer"],
            "risks": [{"text": "Combat feel requires an early experiment.", "level": "amber"}],
        },
        "pitches": pitches,
        "recommended_index": 0,
        "recommendation_rationale": "Best scope and identity.",
    }


def product_identity_result() -> dict:
    return {
        "title": "Ember Circuit",
        "subtitle": "Break the Clock",
        "short_description": "A compact action roguelite about stealing time from a mechanical dungeon.",
        "long_description": "Fight through a changing clockwork prison, build precise synergies, and decide the fate of its final cycle.",
        "package_slug": "ember-circuit",
        "executable_name": "EmberCircuit",
        "display_name": "Ember Circuit",
        "save_namespace": "com.monstermic.ember_circuit",
        "edition": "Standard",
        "branding_vocabulary": ["ember", "clockwork", "cycle"],
        "naming_rules": ["Use short industrial nouns for equipment."],
        "store_metadata": {"genre": "Action Roguelite", "tags": ["Action", "Roguelite", "Offline"]},
    }


def mechanic_result() -> dict:
    return {
        "mechanics": [
            {
                "id": "MEC-0001",
                "name": "Chrono Dash",
                "player_intent": "Evade a telegraphed attack and cross danger quickly.",
                "expected_experience": "Responsive, readable, and skillful movement.",
                "inputs": ["dash action", "movement vector"],
                "preconditions": ["player state is movable", "dash cooldown is zero"],
                "outputs": ["dash velocity", "invulnerability window", "cooldown"],
                "failure_conditions": ["input during stun is ignored"],
                "states": ["ready", "active", "recovery", "cooldown"],
                "transitions": [
                    {"from": "ready", "to": "active", "condition": "dash pressed"},
                    {"from": "active", "to": "recovery", "condition": "0.15 seconds elapsed"},
                    {"from": "recovery", "to": "cooldown", "condition": "0.10 seconds elapsed"},
                    {"from": "cooldown", "to": "ready", "condition": "0.75 seconds elapsed"},
                ],
                "rules": ["Normalize non-zero direction before applying velocity."],
                "processing_order": ["read input", "check state", "apply velocity", "resolve collision", "advance timer"],
                "formulas": [
                    {
                        "name": "dash_distance",
                        "expression": "speed * active_seconds",
                        "unit": "pixels",
                        "default_values": {"speed": 800, "active_seconds": 0.15},
                        "minimum": 96,
                        "maximum": 144,
                        "tuning_range": [108, 132],
                    }
                ],
                "timing": {"active_seconds": 0.15, "recovery_seconds": 0.10, "cooldown_seconds": 0.75},
                "probabilities": [],
                "resource_costs": [],
                "rewards": [],
                "interactions": ["Contact damage is ignored during the invulnerability window."],
                "stacking": "not_applicable",
                "priority": "Dash movement overrides ordinary movement during active state.",
                "cancellation": "Stun cancels recovery but not active movement.",
                "immunities": ["contact damage during active state"],
                "conflicts": ["Cannot attack during active state."],
                "npc_use": "Dash-capable enemies use the same states without invulnerability.",
                "edge_cases": ["Zero direction uses the last non-zero aim direction."],
                "invalid_states": ["active and stunned simultaneously"],
                "feedback": {
                    "visual": "ember trail",
                    "animation": "dash squash",
                    "audio": "short ignition burst",
                    "ui": "cooldown pip",
                    "accessibility": "reduced-flash trail variant",
                    "controller": "light 0.08 second rumble",
                },
                "persistence": "Cooldown resets when a room begins.",
                "determinism": "Uses fixed-step movement and no random values.",
                "debug_instrumentation": ["dash state overlay", "distance counter"],
                "acceptance_tests": ["A default dash travels between 108 and 132 pixels."],
                "success_criteria": ["Dash state transitions are deterministic at 60 physics ticks per second."],
                "playtest_hypotheses": ["Players can intentionally evade a clearly telegraphed attack."],
            }
        ]
    }


def catalog_result() -> dict:
    entry = {
        "id": "CNT-0001",
        "canonical_name": "The Cinder Runner",
        "display_name": "Cinder Runner",
        "localization_key": "content.character.cinder_runner.name",
        "aliases": ["Runner"],
        "pronunciation": "SIN-der RUN-er",
        "kind": "playable_character",
        "category": "character",
        "gameplay_role": "mobile starting character",
        "design_purpose": "Teach movement and Chrono Dash.",
        "description": "A courier who carries the last free ember.",
        "player_behavior": "Moves quickly and converts precise dashes into charge.",
        "mechanic_ids": ["MEC-0001"],
        "stats": {"health": 100, "move_speed": 240},
        "parameters": {"starting_dash_charges": 1},
        "rarity": "starter",
        "tier": 1,
        "acquisition": "Available at first launch.",
        "unlock_conditions": [],
        "spawn_conditions": ["new game"],
        "reward_conditions": [],
        "discovery_conditions": ["start the game"],
        "relationships": ["Carries the World Ember."],
        "required_assets": ["character sprite", "dash animation", "portrait", "voice effort set"],
        "milestone_id": "MS-0001",
        "dod_ids": ["GDD-0001"],
        "launch_status": "must",
        "acceptance_criteria": ["The character can complete the full game path."],
        "playtest_hypotheses": ["New players understand the character's movement advantage."],
    }
    return {
        "name_registry": {
            "id": "NAM-0001",
            "language": "en",
            "entries": [
                {
                    "content_id": "CNT-0001",
                    "canonical_name": "The Cinder Runner",
                    "display_name": "Cinder Runner",
                    "localization_key": "content.character.cinder_runner.name",
                    "category": "character",
                    "aliases": ["Runner"],
                    "pronunciation": "SIN-der RUN-er",
                }
            ],
            "collision_checks": ["canonical_name", "display_name", "localization_key"],
        },
        "content": [entry],
    }


def game_arc_result() -> dict:
    return {
        "id": "BLU-0001",
        "identity": "A precise action roguelite about stealing seconds from a living clock.",
        "audience": "Players who enjoy compact skill-based runs.",
        "pillars": ["precision", "meaningful timing choices", "coherent clockwork world"],
        "promises": ["Every avoidable hit is telegraphed."],
        "non_goals": ["online multiplayer"],
        "release_scope": {"must": ["MEC-0001", "CNT-0001"], "optional": [], "cut": []},
        "player_verbs": ["move", "dash", "attack", "choose", "upgrade"],
        "moment_to_moment_loop": "Read a threat, position, attack, dash, and collect charge.",
        "session_loop": "Enter a route, clear encounters, choose rewards, defeat a keeper, and return to the hub.",
        "long_term_loop": "Recover clock parts, unlock routes, confront the final keeper, and choose an ending.",
        "opening": "The Cinder Runner wakes beside the stopped World Clock and accepts the ember.",
        "first_minute": "Movement, one safe dash obstacle, and the first visible reward teach the central verb.",
        "tutorial": "Context prompts appear once and remain available in the codex.",
        "first_meaningful_decision": "Choose a safe gear reward or a cursed shortcut.",
        "progression_arc": ["Broken Outskirts", "Pendulum Foundry", "Crown Escapement"],
        "major_encounters": ["Outskirts Keeper", "Foundry Keeper", "The Last Horologist"],
        "setbacks": ["A failed run returns carried temporary charge but preserves recovered clock parts."],
        "final_challenge": "Defeat The Last Horologist after restoring three clock parts.",
        "final_prerequisites": ["three clock parts", "Crown Escapement unlocked"],
        "win_conditions": ["The Last Horologist is defeated."],
        "loss_conditions": ["Health reaches zero during a run."],
        "alternate_endings": ["Restart the World Clock", "Break the World Clock"],
        "recovery": "Return to the hub with permanent unlocks intact.",
        "ending": "The chosen clock decision resolves the Runner's promise and changes the hub sky.",
        "credits": "Credits follow the ending and remain replayable from the title screen.",
        "postgame": "New Cycle mode unlocks modifiers, a superboss route, and both ending replays.",
        "replay": "Seeds, characters, routes, and build combinations support new runs.",
        "endgame": "Escalating Cycle modifiers and a final optional keeper extend mastery.",
        "narrative": {
            "applicability": "required",
            "premise": "A courier must decide whether a failing clockwork world deserves another cycle.",
            "character_arcs": ["The Runner changes from obedient courier to independent decision-maker."],
            "acts": ["Awakening", "Restoration", "Decision"],
            "resolution": "The final choice resolves the World Clock and the Runner's obligation.",
        },
        "mechanic_ids": ["MEC-0001"],
        "content_ids": ["CNT-0001"],
        "systems_overview": ["deterministic combat", "run routing", "hub progression", "ending selection"],
        "progression_system": {
            "in_run": "Choose one of three clockwork upgrades after major encounters.",
            "permanent": "Recover clock parts to unlock routes and services.",
            "endgame": "Cycle modifiers increase risk and rewards after the first ending.",
        },
        "economy": {
            "currencies": ["Clock Parts", "Ember Charge"],
            "sources": ["keepers", "risk rooms"],
            "sinks": ["hub services", "route unlocks"],
            "anti_grind_rule": "Every successful keeper defeat guarantees one permanent unlock step.",
        },
        "balance_strategy": {
            "baseline": "All launch content is viable within its declared role.",
            "targets": ["avoidable damage", "no dominant reward in every route"],
            "tuning": "Change values only inside mechanic tuning ranges before revising the blueprint.",
        },
        "difficulty": {
            "curve": "Introduce one new threat role per region before combining roles.",
            "assist": "Optional aim assistance and damage scaling do not alter unlock eligibility.",
            "failure_fairness": "Lethal attacks require a distinct telegraph and reaction window.",
        },
        "ux_information": ["health", "dash cooldown", "route risk", "reward comparison", "objective"],
        "art_direction": "Readable ember silhouettes against desaturated brass machinery.",
        "audio_direction": "Mechanical rhythm communicates danger while ember tones mark safe actions.",
        "save_and_recovery": "Persist at hub entry and room completion; resume the current room seed after interruption.",
        "performance_budgets": {"target_fps": 60, "max_frame_ms": 16.67, "max_load_seconds": 5},
        "platform_and_input": {"platform": "Windows desktop", "inputs": ["keyboard", "controller"]},
        "accessibility": ["remapping", "subtitles", "reduced flashes", "aim assistance"],
        "technical_constraints": ["Godot 4.7", "offline", "60 FPS target"],
        "risks": ["Combat feel requires a prototype playtest."],
        "production_risks": ["Final combat effects depend on a representative vertical-slice approval."],
        "cut_rules": ["Cut optional content before reducing core feedback quality."],
    }


def audit_result(root: Path) -> dict:
    assessments = []
    for path in sorted((root / ".aigame" / "profiles").glob("*.json")):
        profile = json.loads(path.read_text(encoding="utf-8"))
        for item in profile["items"]:
            assessments.append(
                {
                    "profile_id": profile["id"],
                    "item_id": item["id"],
                    "disposition": "required",
                    "rationale": "This item supports the approved player promise.",
                    "scope_refs": ["MEC-0001", "CNT-0001"],
                }
            )
    return {
        "quality_assessment": {"id": "QAS-0001", "assessments": assessments},
        "definition_of_done": {
            "id": "GDD-0001",
            "criteria": [
                {
                    "id": "DONE-0001",
                    "title": "Complete launch scope",
                    "scope_refs": ["MEC-0001", "CNT-0001"],
                    "verification": "automated",
                    "evidence_types": ["test", "runtime", "playtest", "review", "build", "release"],
                    "release_blocking": True,
                    "acceptance_criteria": ["Every must-scope record is implemented and evidenced."],
                    "playtest_hypotheses": ["The complete loop is understandable without developer intervention."],
                }
            ],
        },
        "roadmap": {
            "id": "RMP-0001",
            "milestones": [
                {
                    "id": "MS-0001",
                    "name": "Experiment",
                    "dependencies": [],
                    "deliverables": ["MEC-0001", "CNT-0001"],
                    "exit_gates": ["GDD-0001"],
                    "budget": {"work_items": 2},
                    "cut_lines": ["No optional content in the first experiment."],
                    "detail_level": "task",
                },
                {
                    "id": "MS-0002",
                    "name": "First Playable",
                    "dependencies": ["MS-0001"],
                    "deliverables": ["Complete opening-to-ending placeholder path"],
                    "exit_gates": ["Human first-playable review"],
                    "budget": {"work_items": 1},
                    "cut_lines": ["Preserve the complete path before presentation polish."],
                    "detail_level": "milestone",
                },
            ],
        },
    }


class ConceptBlueprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "game"
        create_game(self.root, "Game", godot_version="4.7", apply=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_approval(self, request: dict, approval_id: str) -> Path:
        path = Path(self.temporary.name) / f"{approval_id}.json"
        path.write_text(json.dumps(approval(request, approval_id)), encoding="utf-8")
        return path

    def _advance_to_arc_task(self) -> None:
        start_concept(self.root, prompt="A small clockwork action roguelite", profiles=["roguelite-v1"], apply=True)
        pitch_gate = submit_concept_task(self.root, "CTK-0001", pitch_result(), apply=True)
        select_concept_pitch(
            self.root,
            "PIT-0001",
            self._write_approval(pitch_gate["approval_request"], "APR-0001"),
            apply=True,
        )
        identity_preview = submit_concept_task(
            self.root, "CTK-0002", product_identity_result(), apply=False
        )
        submit_concept_task(
            self.root,
            "CTK-0002",
            product_identity_result(),
            approval_path=self._write_approval(identity_preview["approval_request"], "APR-0002"),
            apply=True,
        )
        submit_concept_task(self.root, "CTK-0003", mechanic_result(), apply=True)
        submit_concept_task(self.root, "CTK-0004", catalog_result(), apply=True)

    def _advance_to_audit_task(self) -> None:
        self._advance_to_arc_task()
        submit_concept_task(self.root, "CTK-0005", game_arc_result(), apply=True)

    def _advance_to_final_gate(self) -> dict:
        self._advance_to_audit_task()
        return submit_concept_task(self.root, "CTK-0006", audit_result(self.root), apply=True)

    def test_generated_project_contains_v11_contracts_profiles_and_concept_entrypoint(self) -> None:
        schemas = {
            "concept-intake.schema.json",
            "concept-pitch.schema.json",
            "product-identity.schema.json",
            "game-blueprint.schema.json",
            "mechanic-spec.schema.json",
            "content-entry.schema.json",
            "name-registry.schema.json",
            "quality-assessment.schema.json",
            "game-definition-of-done.schema.json",
            "game-roadmap.schema.json",
            "concept-task.schema.json",
        }
        self.assertTrue(all((self.root / ".aigame" / "schemas" / name).is_file() for name in schemas))
        core = json.loads((self.root / ".aigame" / "profiles" / "core-game-v1.json").read_text(encoding="utf-8"))
        roguelite = json.loads((self.root / ".aigame" / "profiles" / "roguelite-v1.json").read_text(encoding="utf-8"))
        self.assertGreater(len(core["items"]), 0)
        self.assertEqual(len(roguelite["items"]), 39)
        self.assertTrue((self.root / ".aigame" / "state" / "concept.json").is_file())
        self.assertIn("aigame concept next", (self.root / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertTrue((self.root / ".aigame" / "agent-skills" / "build-game-concept" / "SKILL.md").is_file())

    def test_start_is_dry_run_safe_and_treats_prompt_as_inert_data(self) -> None:
        marker = self.root / "owned.txt"
        prompt = f'Clock game; Set-Content -LiteralPath "{marker}" -Value owned'
        preview = start_concept(self.root, prompt=prompt, profiles=["roguelite-v1"], apply=False)
        self.assertEqual(preview["status"], "dry_run")
        self.assertFalse((self.root / "work" / "concept" / "CIN-0001.json").exists())
        started = start_concept(self.root, prompt=prompt, profiles=["roguelite-v1"], apply=True)
        self.assertEqual(started["concept_task"]["stage"], "pitching")
        intake = json.loads((self.root / "work" / "concept" / "CIN-0001.json").read_text(encoding="utf-8"))
        self.assertEqual(intake["prompt"], prompt)
        self.assertFalse(marker.exists())

    def test_pitch_submission_requires_exactly_three_and_selection_approval(self) -> None:
        start_concept(self.root, prompt="Clock roguelite", profiles=["roguelite-v1"], apply=True)
        invalid = pitch_result()
        invalid["pitches"].pop()
        with self.assertRaisesRegex(WorkflowError, "exactly three"):
            submit_concept_task(self.root, "CTK-0001", invalid, apply=True)
        gate = submit_concept_task(self.root, "CTK-0001", pitch_result(), apply=True)
        self.assertEqual(gate["status"], "needs_human")
        resumed_gate = next_concept_task(self.root)
        self.assertEqual(
            resumed_gate["approval_requests"]["PIT-0002"]["decision"],
            "select_concept_direction:PIT-0002",
        )
        intake = json.loads((self.root / "work" / "concept" / "CIN-0001.json").read_text(encoding="utf-8"))
        self.assertEqual(intake["audience"], "Players who enjoy compact skill-based action roguelites.")
        self.assertEqual(intake["risks"][0]["level"], "amber")
        with self.assertRaisesRegex(WorkflowError, "approval"):
            select_concept_pitch(self.root, "PIT-0001", None, apply=True)
        with self.assertRaisesRegex(WorkflowError, "approval"):
            select_concept_pitch(
                self.root,
                "PIT-0002",
                self._write_approval(gate["approval_request"], "APR-0099"),
                apply=True,
            )
        selected = select_concept_pitch(
            self.root,
            "PIT-0001",
            self._write_approval(gate["approval_request"], "APR-0001"),
            apply=True,
        )
        self.assertEqual(selected["concept_task"]["stage"], "product_identity")

    def test_incomplete_mechanics_and_duplicate_catalog_names_are_rejected(self) -> None:
        start_concept(self.root, prompt="Clock roguelite", profiles=["roguelite-v1"], apply=True)
        gate = submit_concept_task(self.root, "CTK-0001", pitch_result(), apply=True)
        select_concept_pitch(self.root, "PIT-0001", self._write_approval(gate["approval_request"], "APR-0001"), apply=True)
        identity_preview = submit_concept_task(self.root, "CTK-0002", product_identity_result(), apply=False)
        submit_concept_task(
            self.root, "CTK-0002", product_identity_result(),
            approval_path=self._write_approval(identity_preview["approval_request"], "APR-0002"), apply=True,
        )
        incomplete = mechanic_result()
        incomplete["mechanics"][0].pop("formulas")
        with self.assertRaisesRegex(WorkflowError, "formulas"):
            submit_concept_task(self.root, "CTK-0003", incomplete, apply=True)
        inconsistent = mechanic_result()
        inconsistent["mechanics"][0]["formulas"][0]["tuning_range"] = [150, 90]
        with self.assertRaisesRegex(WorkflowError, "tuning range"):
            submit_concept_task(self.root, "CTK-0003", inconsistent, apply=True)
        submit_concept_task(self.root, "CTK-0003", mechanic_result(), apply=True)
        duplicate = catalog_result()
        second = dict(duplicate["content"][0])
        second["id"] = "CNT-0002"
        duplicate["content"].append(second)
        with self.assertRaisesRegex(WorkflowError, "Duplicate canonical name"):
            submit_concept_task(self.root, "CTK-0004", duplicate, apply=True)

        mismatch = catalog_result()
        mismatch["name_registry"]["entries"][0]["canonical_name"] = "Wrong Name"
        with self.assertRaisesRegex(WorkflowError, "does not match content"):
            submit_concept_task(self.root, "CTK-0004", mismatch, apply=True)

    def test_product_identity_approval_gate_is_resumable(self) -> None:
        start_concept(self.root, prompt="Clock roguelite", profiles=["roguelite-v1"], apply=True)
        gate = submit_concept_task(self.root, "CTK-0001", pitch_result(), apply=True)
        select_concept_pitch(
            self.root,
            "PIT-0001",
            self._write_approval(gate["approval_request"], "APR-0001"),
            apply=True,
        )
        identity_gate = submit_concept_task(
            self.root,
            "CTK-0002",
            product_identity_result(),
            apply=True,
        )
        self.assertEqual(identity_gate["status"], "needs_human")
        resumed = next_concept_task(self.root)["concept_task"]
        self.assertEqual(resumed["pending_result"]["title"], "Ember Circuit")
        self.assertEqual(resumed["approval_request"], identity_gate["approval_request"])

    def test_incomplete_game_systems_are_rejected_before_the_quality_audit(self) -> None:
        self._advance_to_arc_task()
        incomplete = game_arc_result()
        incomplete.pop("economy")
        with self.assertRaisesRegex(WorkflowError, "economy"):
            submit_concept_task(self.root, "CTK-0005", incomplete, apply=True)
        inconsistent = game_arc_result()
        inconsistent["release_scope"] = {
            "must": ["CNT-0001"],
            "optional": ["MEC-0001"],
            "cut": [],
        }
        with self.assertRaisesRegex(WorkflowError, "launch status"):
            submit_concept_task(self.root, "CTK-0005", inconsistent, apply=True)

    def test_failed_quality_audit_remains_retryable(self) -> None:
        self._advance_to_audit_task()
        invalid = audit_result(self.root)
        invalid["quality_assessment"]["assessments"].pop()
        with self.assertRaisesRegex(WorkflowError, "quality profile item"):
            submit_concept_task(self.root, "CTK-0006", invalid, apply=True)
        task = next_concept_task(self.root)["concept_task"]
        self.assertEqual(task["id"], "CTK-0006")
        self.assertEqual(task["status"], "ready")

    def test_interrupted_transition_keeps_the_active_task_retryable(self) -> None:
        self._advance_to_arc_task()
        with mock.patch("aigame.concept._save_state", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                submit_concept_task(self.root, "CTK-0005", game_arc_result(), apply=True)
        resumed = next_concept_task(self.root)["concept_task"]
        self.assertEqual(resumed["id"], "CTK-0005")
        self.assertEqual(resumed["status"], "ready")
        completed = submit_concept_task(
            self.root,
            "CTK-0005",
            game_arc_result(),
            apply=True,
        )
        self.assertEqual(completed["concept_task"]["id"], "CTK-0006")

    def test_interrupted_finalization_rebuilds_without_duplicate_work(self) -> None:
        final_gate = self._advance_to_final_gate()
        approval_path = self._write_approval(final_gate["approval_request"], "APR-0003")
        with mock.patch("aigame.concept._save_state", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                finalize_concept(self.root, approval_path, apply=True)
        final = finalize_concept(self.root, approval_path, apply=True)
        requirement_paths = list((self.root / "work" / "requirements").glob("REQ-*.json"))
        item_paths = list((self.root / "work" / "items").glob("WI-*.json"))
        self.assertEqual(
            len(requirement_paths),
            1 + len(final["materialized"]["requirements"]),
        )
        self.assertEqual(len(item_paths), 1 + len(final["materialized"]["work_items"]))

    def test_finalize_rejects_approval_bound_before_blueprint_commit(self) -> None:
        subprocess.run(["git", "init", "-b", "main"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-m", "bootstrap"], cwd=self.root, check=True, capture_output=True)
        final_gate = self._advance_to_final_gate()
        self.assertEqual(final_gate["gate"], "commit_blueprint_inputs")
        stale_request = {
            "scope_hash": "0" * 64,
            "commit_sha": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip(),
            "decision": "approve_complete_game_blueprint",
        }
        with self.assertRaisesRegex(WorkflowError, "Commit the canonical blueprint inputs"):
            finalize_concept(
                self.root,
                self._write_approval(stale_request, "APR-0003"),
                apply=True,
            )
        self.assertFalse((self.root / "evidence" / "approvals" / "APR-0003.json").exists())

    def test_human_mode_requires_committed_blueprint_inputs_before_approval(self) -> None:
        subprocess.run(["git", "init", "-b", "main"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-m", "bootstrap"], cwd=self.root, check=True, capture_output=True)

        pending = self._advance_to_final_gate()
        self.assertEqual(pending["status"], "blocked")
        self.assertEqual(pending["gate"], "commit_blueprint_inputs")
        self.assertEqual(next_concept_task(self.root)["gate"], "commit_blueprint_inputs")

        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-m", "commit blueprint inputs"], cwd=self.root, check=True, capture_output=True)
        gate = next_concept_task(self.root)
        self.assertEqual(gate["status"], "needs_human")
        self.assertEqual(
            gate["approval_request"]["commit_sha"],
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip(),
        )
        final = finalize_concept(
            self.root,
            self._write_approval(gate["approval_request"], "APR-0003"),
            apply=True,
        )
        self.assertEqual(final["status"], "passed")

    def test_complete_flow_renders_blueprint_materializes_work_and_resumes(self) -> None:
        final_gate = self._advance_to_final_gate()
        self.assertEqual(final_gate["status"], "needs_human")
        waiting = next_concept_task(self.root)
        self.assertEqual(waiting["status"], "needs_human")
        preview = render_concept(self.root, apply=False)
        self.assertEqual(preview["status"], "dry_run")
        final = finalize_concept(
            self.root,
            self._write_approval(final_gate["approval_request"], "APR-0003"),
            apply=True,
        )
        self.assertEqual(final["status"], "passed")
        report = validate_concept(self.root, require_final=True)
        self.assertEqual(report["status"], "passed", report["errors"])
        blueprint = self.root / "docs" / "blueprint" / "complete-game-blueprint.md"
        mechanics = self.root / "docs" / "blueprint" / "mechanics.md"
        catalog = self.root / "docs" / "blueprint" / "launch-content-catalog.md"
        self.assertIn("The Last Horologist", blueprint.read_text(encoding="utf-8"))
        self.assertIn("Clock Parts", blueprint.read_text(encoding="utf-8"))
        self.assertIn("light 0.08 second rumble", mechanics.read_text(encoding="utf-8"))
        self.assertIn("character sprite", catalog.read_text(encoding="utf-8"))
        self.assertTrue((self.root / "work" / "requirements" / "REQ-0002.json").is_file())
        self.assertTrue((self.root / "work" / "items" / "WI-0002.json").is_file())
        requirements = [json.loads(path.read_text(encoding="utf-8")) for path in (self.root / "work" / "requirements").glob("REQ-*.json")]
        self.assertTrue(any(record.get("quality_profile_ref") == "core-game-v1/CORE-001" for record in requirements))
        self.assertEqual(next_concept_task(self.root)["status"], "finalized")
        packet = build_context(self.root, "WI-0002")
        concept_ids = {record["id"] for record in packet["concept_records"]}
        self.assertTrue({"MEC-0001", "BLU-0001", "GDD-0001"}.issubset(concept_ids))
        project_report = validate_project(self.root)
        self.assertEqual(project_report["status"], "passed", project_report["errors"])
        self.assertGreater(project_report["counts"]["concept_records"], 0)

    def test_missing_profile_classification_and_roadmap_cycle_block_finalization(self) -> None:
        final_gate = self._advance_to_final_gate()
        assessment_path = self.root / "work" / "concept" / "QAS-0001.json"
        assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
        assessment["assessments"].pop()
        assessment["input_fingerprint"] = fingerprint(
            {key: value for key, value in assessment.items() if key != "input_fingerprint"}
        )
        assessment_path.write_text(json.dumps(assessment), encoding="utf-8")
        report = validate_concept(self.root, require_final=True)
        self.assertTrue(any("quality profile item" in error for error in report["errors"]))
        project_report = validate_project(self.root)
        self.assertTrue(any("quality profile item" in error for error in project_report["errors"]))
        with self.assertRaises(WorkflowError):
            finalize_concept(
                self.root,
                self._write_approval(final_gate["approval_request"], "APR-0003"),
                apply=True,
            )

    def test_vendored_profile_drift_invalidates_the_workflow_lock(self) -> None:
        profile = self.root / ".aigame" / "vendor" / "aigame" / "profiles" / "roguelite-v1.json"
        value = json.loads(profile.read_text(encoding="utf-8"))
        value["title"] = "Unreviewed drift"
        profile.write_text(json.dumps(value), encoding="utf-8")
        report = validate_project(self.root)
        self.assertTrue(any("workflow snapshot checksum" in error for error in report["errors"]))

    def test_blueprint_input_change_invalidates_the_bound_approval(self) -> None:
        gate = self._advance_to_final_gate()
        finalize_concept(self.root, self._write_approval(gate["approval_request"], "APR-0003"), apply=True)
        blueprint_path = self.root / "work" / "concept" / "BLU-0001.json"
        blueprint = json.loads(blueprint_path.read_text(encoding="utf-8"))
        blueprint["ending"] = "A different, unapproved ending."
        blueprint["input_fingerprint"] = fingerprint(
            {key: value for key, value in blueprint.items() if key != "input_fingerprint"}
        )
        blueprint_path.write_text(json.dumps(blueprint), encoding="utf-8")
        report = validate_project(self.root)
        self.assertTrue(any("blueprint approval scope" in error for error in report["errors"]))

    def test_revision_invalidates_blueprint_approval_and_preserves_legacy_docs(self) -> None:
        legacy = self.root / "docs" / "game_design.md"
        before = legacy.read_text(encoding="utf-8")
        gate = self._advance_to_final_gate()
        finalize_concept(self.root, self._write_approval(gate["approval_request"], "APR-0003"), apply=True)
        change_path = Path(self.temporary.name) / "change.json"
        change_path.write_text(json.dumps({"summary": "Change the final choice", "identity_change": True}), encoding="utf-8")
        revised = revise_concept(self.root, change_path, apply=True)
        self.assertEqual(revised["status"], "rework")
        self.assertEqual(legacy.read_text(encoding="utf-8"), before)
        state = json.loads((self.root / ".aigame" / "state" / "concept.json").read_text(encoding="utf-8"))
        self.assertIsNone(state["blueprint_approval_id"])
        self.assertIsNone(state["direction_approval_id"])
        requirement = json.loads((self.root / "work" / "requirements" / "REQ-0002.json").read_text(encoding="utf-8"))
        work_item = json.loads((self.root / "work" / "items" / "WI-0002.json").read_text(encoding="utf-8"))
        self.assertEqual(requirement["status"], "deprecated")
        self.assertEqual(work_item["status"], "cancelled")

    def test_revision_can_restart_at_the_affected_concept_stage(self) -> None:
        gate = self._advance_to_final_gate()
        finalize_concept(self.root, self._write_approval(gate["approval_request"], "APR-0003"), apply=True)
        change_path = Path(self.temporary.name) / "mechanics-change.json"
        change_path.write_text(
            json.dumps(
                {
                    "summary": "Replace dash timing and all dependent content values.",
                    "restart_stage": "mechanics",
                    "identity_change": False,
                }
            ),
            encoding="utf-8",
        )
        revised = revise_concept(self.root, change_path, apply=True)
        self.assertEqual(revised["concept_task"]["stage"], "mechanics")
        state = json.loads((self.root / ".aigame" / "state" / "concept.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(state["direction_approval_id"])
        self.assertIsNotNone(state["product_identity_approval_id"])


class ConceptCliTests(unittest.TestCase):
    def _run(self, arguments: list[str]) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(arguments)
        return code, json.loads(output.getvalue())

    def test_concept_commands_are_exposed_and_next_prioritizes_concept_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            code, preview = self._run(
                ["concept", "start", "--project", str(root), "--prompt", "Clock roguelite", "--profile", "roguelite-v1", "--json"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(preview["status"], "dry_run")
            code, started = self._run(
                ["concept", "start", "--project", str(root), "--prompt", "Clock roguelite", "--profile", "roguelite-v1", "--apply", "--json"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(started["concept_task"]["id"], "CTK-0001")
            code, selected = self._run(["next", "--project", str(root), "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(selected["concept_task"]["id"], "CTK-0001")
            code, task = self._run(["concept", "next", "--project", str(root), "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(task["concept_task"]["stage"], "pitching")

    def test_invalid_concept_submission_uses_validation_failure_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            create_game(root, "Game", godot_version="4.7", apply=True)
            self._run(
                [
                    "concept",
                    "start",
                    "--project",
                    str(root),
                    "--prompt",
                    "Clock game",
                    "--apply",
                    "--json",
                ]
            )
            result_path = Path(temporary) / "invalid.json"
            result_path.write_text("{}", encoding="utf-8")
            code, payload = self._run(
                [
                    "concept",
                    "submit",
                    "CTK-0001",
                    "--project",
                    str(root),
                    "--result",
                    str(result_path),
                    "--apply",
                    "--json",
                ]
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "failed")


if __name__ == "__main__":
    unittest.main()
