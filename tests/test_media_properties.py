from __future__ import annotations

import hashlib
import json
import unittest

from hypothesis import given, settings, strategies as st

from aigame.core import WorkflowError, fingerprint
from aigame.media import (
    BODY_FAMILY_DEFINITIONS,
    _indexed_png,
    _planning_requests,
    _records_for_plan,
)
from tests.test_concept_blueprint import media_direction_result, media_request_for_need


FAMILY_TAGS = {
    "humanoid": (["biped"], ["cloth"], ["upright"]),
    "serpentine": (["elongated", "limbless"], ["scaled"], ["s_curve"]),
    "quadruped": (["four_legged"], ["furred"], ["low_profile"]),
    "winged": (["winged"], ["feathered"], ["wide_span"]),
    "amorphous": (["blob"], ["gelatinous"], ["irregular"]),
    "mechanical_vehicle": (["wheeled"], ["metal"], ["chassis"]),
}


def _persisted(value: dict, *, status: str = "approved") -> dict:
    result = {
        "schema_version": "1.0",
        "revision": 1,
        "status": status,
        "created_at": "2026-07-16T00:00:00Z",
        "updated_at": "2026-07-16T00:00:00Z",
        **value,
    }
    result["input_fingerprint"] = fingerprint(result)
    return result


def _direction() -> dict:
    return _persisted({"id": "MDR-0001", **media_direction_result()})


def _actor_request(
    request_id: str,
    source_ref: str,
    family: str,
    seed: int,
) -> dict:
    need = {
        "source_ref": source_ref,
        "purpose": "actor sprite",
        "kind": "sprite",
        "required": True,
    }
    request = media_request_for_need(int(request_id.split("-")[1]), need)
    body, surface, silhouette = FAMILY_TAGS[family]
    request["specification"].update(
        {
            "body_family": family,
            "body_tags": body,
            "surface_tags": surface,
            "silhouette_tags": silhouette,
            "equipment_tags": [],
        }
    )
    request["control"]["seed"] = seed
    return _persisted(request)


def _records(requests: list[dict]) -> dict:
    needs = [
        {
            "source_ref": request["source_refs"][0],
            "purpose": request["purpose"],
            "kind": request["family"],
            "required": True,
        }
        for request in requests
    ]
    planned = _planning_requests(needs, requests)
    return _records_for_plan(planned, "a" * 64, [], _direction())


class StructuredMediaProperties(unittest.TestCase):
    @settings(max_examples=24, deadline=None)
    @given(
        family=st.sampled_from(sorted(FAMILY_TAGS)),
        seed=st.integers(min_value=-(2**31), max_value=2**31 - 1),
    )
    def test_selected_parts_always_match_family_and_requested_tags(
        self, family: str, seed: int
    ) -> None:
        request = _actor_request("ARQ-0001", "CNT-0001.required_assets[0]", family, seed)
        records = _records([request])
        recipe = records["recipes"][0]
        parts = {
            part["id"]: part for part in records["parts"]
            if part["id"] in recipe["source_part_ids"]
        }
        self.assertTrue(parts)
        self.assertTrue(
            all(part["compatible_body_families"] == [family] for part in parts.values())
        )
        requested_tags = {
            tag
            for field in ("body_tags", "surface_tags", "silhouette_tags", "equipment_tags")
            for tag in request["specification"][field]
        }
        selected_tags = {tag for part in parts.values() for tag in part["tags"]}
        self.assertTrue(requested_tags.issubset(selected_tags))
        forbidden = set(BODY_FAMILY_DEFINITIONS[family]["forbidden_slots"])
        self.assertFalse(forbidden & {part["slot"] for part in parts.values()})

    @settings(max_examples=18, deadline=None)
    @given(
        family=st.sampled_from(sorted(FAMILY_TAGS)),
        first_seed=st.integers(),
        second_seed=st.integers(),
    )
    def test_seed_never_changes_locked_family_anchors_equipment_or_timing(
        self, family: str, first_seed: int, second_seed: int
    ) -> None:
        first = _records([
            _actor_request("ARQ-0001", "CNT-0001.required_assets[0]", family, first_seed)
        ])
        second = _records([
            _actor_request("ARQ-0001", "CNT-0001.required_assets[0]", family, second_seed)
        ])
        first_recipe = first["recipes"][0]
        second_recipe = second["recipes"][0]
        for field in ("body_family", "required_directions", "layer_choices"):
            self.assertEqual(
                first_recipe["parameters"][field], second_recipe["parameters"][field]
            )
        self.assertEqual(first_recipe["source_part_ids"], second_recipe["source_part_ids"])
        self.assertEqual(
            [part["anchors"] for part in first["parts"]],
            [part["anchors"] for part in second["parts"]],
        )

    @settings(max_examples=12, deadline=None)
    @given(family=st.sampled_from(sorted(FAMILY_TAGS)), seed=st.integers())
    def test_identical_brief_and_seed_reproduce_records_and_hashes(
        self, family: str, seed: int
    ) -> None:
        request = _actor_request("ARQ-0001", "CNT-0001.required_assets[0]", family, seed)
        first = _records([request])
        second = _records([json.loads(json.dumps(request))])
        self.assertEqual(first, second)
        recipe = first["recipes"][0]
        selected = [
            part for part in first["parts"] if part["id"] in recipe["source_part_ids"]
        ]
        style = first["styles"][0]
        first_hash = hashlib.sha256(_indexed_png(recipe, style, seed, selected)).hexdigest()
        second_hash = hashlib.sha256(_indexed_png(recipe, style, seed, selected)).hexdigest()
        self.assertEqual(first_hash, second_hash)

    @settings(max_examples=16, deadline=None)
    @given(order=st.permutations((0, 1, 2)))
    def test_request_input_order_does_not_change_canonical_plans(self, order: tuple[int, ...]) -> None:
        requests = [
            _actor_request("ARQ-0001", "CNT-0001.required_assets[0]", "humanoid", 1),
            _actor_request("ARQ-0002", "CNT-0002.required_assets[0]", "serpentine", 2),
            _actor_request("ARQ-0003", "CNT-0003.required_assets[0]", "quadruped", 3),
        ]
        canonical = _records(requests)
        reordered = _records([requests[index] for index in order])
        self.assertEqual(canonical, reordered)

    @settings(max_examples=12, deadline=None)
    @given(changed_seed=st.integers().filter(lambda value: value != 1))
    def test_changing_one_brief_invalidates_only_its_dependent_recipe(
        self, changed_seed: int
    ) -> None:
        first_requests = [
            _actor_request("ARQ-0001", "CNT-0001.required_assets[0]", "humanoid", 1),
            _actor_request("ARQ-0002", "CNT-0002.required_assets[0]", "serpentine", 2),
        ]
        before = _records(first_requests)
        changed = json.loads(json.dumps(first_requests[0]))
        changed["control"]["seed"] = changed_seed
        changed["input_fingerprint"] = fingerprint(
            {key: value for key, value in changed.items() if key != "input_fingerprint"}
        )
        after = _records([changed, first_requests[1]])
        before_by_request = {
            recipe["parameters"]["request_id"]: recipe["input_fingerprint"]
            for recipe in before["recipes"]
        }
        after_by_request = {
            recipe["parameters"]["request_id"]: recipe["input_fingerprint"]
            for recipe in after["recipes"]
        }
        self.assertNotEqual(before_by_request["ARQ-0001"], after_by_request["ARQ-0001"])
        self.assertEqual(before_by_request["ARQ-0002"], after_by_request["ARQ-0002"])

    @settings(max_examples=12, deadline=None)
    @given(unsupported_tag=st.text(min_size=1).filter(lambda value: value not in {tag for tags in FAMILY_TAGS.values() for group in tags for tag in group}))
    def test_unsupported_tags_block_before_any_output_can_be_compiled(
        self, unsupported_tag: str
    ) -> None:
        request = _actor_request("ARQ-0001", "CNT-0001.required_assets[0]", "serpentine", 1)
        request["specification"]["equipment_tags"] = [unsupported_tag]
        request["input_fingerprint"] = fingerprint(
            {key: value for key, value in request.items() if key != "input_fingerprint"}
        )
        with self.assertRaises(WorkflowError):
            _records([request])


if __name__ == "__main__":
    unittest.main()
