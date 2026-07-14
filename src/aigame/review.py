from __future__ import annotations

import re
from typing import Any


class ReviewRejected(RuntimeError):
    pass


def validate_review(report: dict[str, Any], *, expected_commit: str) -> dict[str, Any]:
    failures: list[str] = []
    if report.get("reviewed_commit") != expected_commit:
        failures.append("reviewed commit does not match the expected head")
    if report.get("producer") == report.get("reviewer"):
        failures.append("producer and reviewer must be distinct identities")
    dimensions = report.get("dimensions", {})
    if not dimensions:
        failures.append("review dimensions are missing")
    for name, score in dimensions.items():
        if not isinstance(score, (int, float)) or score < 90:
            failures.append(f"dimension {name!r} must score at least 90")
    if not isinstance(report.get("overall"), (int, float)) or report.get("overall", 0) < 90:
        failures.append("overall review score must be at least 90")
    if report.get("findings"):
        failures.append("review has open findings")
    hashes = report.get("evidence_hashes", [])
    if not hashes or any(not re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in hashes):
        failures.append("review evidence hashes are missing or invalid")
    if failures:
        raise ReviewRejected("; ".join(failures))
    return {
        "schema_version": "1.0",
        "status": "passed",
        "reviewed_commit": expected_commit,
        "reviewer": report["reviewer"],
        "overall": report["overall"],
    }
