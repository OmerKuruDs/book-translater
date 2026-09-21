"""Redaction filter (design doc 02, 9.5; CR-11): secrets and DeepL Free keys, never job ids."""

from __future__ import annotations

import logging

from book_translator.logging_setup import REDACTED, RedactionFilter

FREE_KEY = "0123abcd-1111-2222-3333-444455556666:fx"
PRO_KEY = "9999abcd-1111-2222-3333-444455556666"
JOB_ID = "9f1c2d3e-4b5a-6c7d-8e9f-0a1b2c3d4e5f"
SHA256 = "0123abcd11112222333344445555666677778888999900001111222233334444"


def test_configured_secrets_and_free_key_shape_are_masked() -> None:
    redaction = RedactionFilter([PRO_KEY, "abc def:fx"])
    text = (
        f"free={FREE_KEY} pro={PRO_KEY} odd=abc def:fx job={JOB_ID} "
        f"path=C:\\out\\{JOB_ID}\\state-archive\\20260918T120000Z sha={SHA256}"
    )
    out = redaction.redact_text(text)
    assert FREE_KEY not in out and PRO_KEY not in out and "abc def:fx" not in out
    assert out.count(REDACTED) == 3
    assert JOB_ID in out and f"{JOB_ID}\\state-archive" in out and SHA256 in out


def test_unconfigured_job_ids_and_bare_uuids_survive() -> None:
    redaction = RedactionFilter([])
    out = redaction.redact_text(f"job {JOB_ID} archived; key {FREE_KEY}; uuid {PRO_KEY}")
    assert JOB_ID in out and PRO_KEY in out and FREE_KEY not in out
    assert redaction.redact_text(f"{FREE_KEY}extra") == f"{FREE_KEY}extra"  # not a key boundary


def test_filter_redacts_message_args_and_cause() -> None:
    redaction = RedactionFilter([PRO_KEY])
    record = logging.LogRecord(
        "book_translator", logging.INFO, __file__, 1, "key %s job %s", (PRO_KEY, JOB_ID), None
    )
    record.cause = f"AuthorizationException({FREE_KEY})"
    assert redaction.filter(record)
    assert record.getMessage() == f"key {REDACTED} job {JOB_ID}"
    assert FREE_KEY not in str(record.cause)
