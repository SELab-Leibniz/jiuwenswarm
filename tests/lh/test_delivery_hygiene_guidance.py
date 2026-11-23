# Copyright (c) 2026 Chrys. All rights reserved.

"""Worker and acceptance-reviewer prompts carry the delivery hygiene gate."""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.lh import controller, worker_evidence


def test_worker_and_reviewer_guidance_include_delivery_hygiene():
    worker = worker_evidence.worker_evidence_guidance().lower()
    reviewer = worker_evidence.reviewer_delivery_hygiene_guidance().lower()
    for text in (worker, reviewer):
        assert "absolute path" in text
        assert "reference" in text and "expected-output" in text
        assert "outside the workspace" in text
    assert "blocking" in reviewer
    assert "worker" in reviewer
    for text in (worker, reviewer):
        assert "self-contained" in text
        assert "production hostnames" in text
        assert "endpoint" in text
        assert "official client sdk" in text
        assert "deliverable tree" in text
        assert "tests, fixtures and scripts" in text
        assert "outside the deliverable tree" in text
        assert "protocol-level http" in text
        assert "tests and verification" not in text
        assert "only for tests" not in text
    assert worker_evidence.DELIVERY_HYGIENE_GUIDANCE_VERSION == "delivery-hygiene/v3"


def test_controller_worker_and_review_context_carry_the_hygiene_clauses():
    state = SimpleNamespace(
        contract={"goal": "synthetic public requirement"},
        constraints=["synthetic constraint"],
        acceptance_brief={"source": "synthetic"},
    )
    assert "Delivery hygiene" in controller._p0_prompt(state)
    assert "Delivery hygiene gate" in controller._first_review_context(state, "feedback")
