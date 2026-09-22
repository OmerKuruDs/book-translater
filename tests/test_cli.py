"""CLI contract: exit-code mapping, --help, fail-fast settings, status/listings (design 9)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from typer.testing import CliRunner

from book_translator import cli
from book_translator.config import load_settings
from book_translator.database.repository import JobPatch, JobRepository, JobSpec
from book_translator.database.session import close_database, open_database
from book_translator.domain.models import JobSummary, StatusCounts
from book_translator.domain.result import AppError, Err, ErrorCode, ErrorScope, Ok, unwrap
from book_translator.glossary.manager import effective_glossary
from book_translator.pipeline import orchestrator as orchestrator_module
from book_translator.pipeline.orchestrator import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_LOCKED,
    EXIT_MISMATCH,
    EXIT_PARTIAL,
    EXIT_PAUSED,
    EXIT_SUCCESS,
    exit_code_for,
    worst_exit,
)
from book_translator.translators.base import BaseTranslator, ProviderGlossary
from tests.conftest import DEFAULT_CAPABILITIES

runner = CliRunner()
WIDE = {"COLUMNS": "200", "TERM": "dumb"}
#: A glossary discovered from a page of prose has no *approved* entry, so the effective
#: glossary of the cleanup tests is the empty one - and its hash is a constant.
EMPTY_GLOSSARY_HASH = effective_glossary([]).glossary_hash


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    monkeypatch.delenv("DEEPL_SERVER_URL", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_API", raising=False)
    for name in list(__import__("os").environ):
        if name.startswith("BOOK_TRANSLATOR_"):
            monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# Exit-code mapping (9.3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ErrorCode.STATE_LOCKED, EXIT_LOCKED),
        (ErrorCode.STATE_HASH_MISMATCH, EXIT_MISMATCH),
        (ErrorCode.STATE_PROVIDER_MISMATCH, EXIT_MISMATCH),
        (ErrorCode.STATE_SCHEMA_INCOMPATIBLE, EXIT_MISMATCH),
        (ErrorCode.INTERRUPTED, EXIT_INTERRUPTED),
        (ErrorCode.EXPORT_REFUSED_PARTIAL, EXIT_PARTIAL),
        (ErrorCode.EXPORTER_UNAVAILABLE, EXIT_PARTIAL),
        (ErrorCode.PROVIDER_QUOTA, EXIT_PAUSED),
        (ErrorCode.PROVIDER_AUTH, EXIT_PAUSED),
        (ErrorCode.GLOSSARY_BIND_FAILED, EXIT_PAUSED),
        (ErrorCode.PROVIDER_CONFIG, EXIT_FAILURE),
        (ErrorCode.NO_TEXT_LAYER, EXIT_FAILURE),
        (ErrorCode.EXTRACTION_FAILED, EXIT_FAILURE),
        (ErrorCode.EXPORT_FAILED, EXIT_FAILURE),
        (ErrorCode.INTERNAL, EXIT_FAILURE),
    ],
)
def test_exit_code_for_table(code: ErrorCode, expected: int) -> None:
    assert exit_code_for(AppError(code, "m", ErrorScope.USER)) == expected


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ([], 0),
        ([0, 2], 2),
        ([2, 130], 130),
        ([130, 5], 5),
        ([5, 4], 4),
        ([4, 3], 3),
        ([3, 1], 1),
        ([0, 0], 0),
        ([2, 7], 1),
    ],
)
def test_worst_exit_severity_order(codes: List[int], expected: int) -> None:
    assert worst_exit(codes) == expected


# --------------------------------------------------------------------------- #
# --help
# --------------------------------------------------------------------------- #


def test_help_works_without_api_key_and_lists_commands() -> None:
    result = runner.invoke(cli.app, ["--help"], env=WIDE)
    assert result.exit_code == 0, result.output
    for command in ("run", "extract", "glossary", "translate", "export", "status",
                    "providers", "extractors", "exporters"):
        assert command in result.output


def test_translate_help_shows_defaults_and_env_vars() -> None:
    result = runner.invoke(cli.app, ["translate", "--help"], env=WIDE)
    assert result.exit_code == 0, result.output
    text = result.output
    assert "--provider" in text and "deepl" in text
    assert "BOOK_TRANSLATOR_PROVIDER" in text
    assert "--concurrency" in text and "BOOK_TRANSLATOR_CONCURRENCY" in text
    for option in ("--max-retries", "--chunk-min", "--chunk-max", "--retry-failed",
                   "--allow-provider-switch", "--require-glossary", "--strict-glossary",
                   "--grace-seconds", "--force-unlock", "--dry-run", "--limit", "--json",
                   "--quiet", "--no-progress", "--log-level", "--log-file"):
        assert option in text, option


def test_run_help_lists_export_and_extract_options() -> None:
    result = runner.invoke(cli.app, ["run", "--help"], env=WIDE)
    assert result.exit_code == 0, result.output
    for option in ("--input", "--output", "--extractor", "--allow-non-english",
                   "--min-chars-per-page", "--fresh", "--format", "--allow-partial",
                   "--title", "--author", "--css", "--pdf-font", "--chapter-level",
                   "--pdf-engine", "--pdf-page-size", "--pdf-margin"):
        assert option in result.output, option


def test_export_help_lists_the_f3_pdf_options() -> None:
    """Design doc 06, 7.1: ``--pdf-engine``, ``--pdf-page-size``, ``--pdf-margin`` (F3)."""
    result = runner.invoke(cli.app, ["export", "--help"], env=WIDE)
    assert result.exit_code == 0, result.output
    text = result.output
    for option in ("--pdf-engine", "--pdf-page-size", "--pdf-margin", "--pdf-font", "--css"):
        assert option in text, option
    assert "native" in text and "weasyprint" in text
    assert "BOOK_TRANSLATOR_PDF_ENGINE" in text and "BOOK_TRANSLATOR_PDF_MARGIN" in text
    # Addendum A: the page design follows the source; no layout default the user never chose
    assert "source" in text
    assert "A5" not in text and "18mm" not in text and "12pt" not in text and "12 pt" not in text


def test_export_rejects_a_bad_pdf_margin_and_engine_before_running(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    bad = runner.invoke(
        cli.app, ["export", "-o", str(out), "--pdf-margin", "1mm 2mm 3mm", "--json"], env=WIDE
    )
    assert bad.exit_code == EXIT_FAILURE, bad.output
    payload = json.loads(bad.stdout)
    assert payload["ok"] is False and payload["error"]["scope"] == "user"
    assert "--pdf-margin" in payload["error"]["message"]
    usage = runner.invoke(cli.app, ["export", "-o", str(out), "--pdf-engine", "latex"], env=WIDE)
    assert usage.exit_code == 2 and "latex" in usage.output


def test_export_forwards_the_f3_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    def fake_export(self: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return Err(AppError(ErrorCode.STATE_NOT_FOUND, "no state", ErrorScope.USER))

    monkeypatch.setattr(cli.Orchestrator, "export", fake_export)
    out = tmp_path / "out"
    out.mkdir()
    result = runner.invoke(
        cli.app,
        ["export", "-o", str(out), "--pdf-engine", "weasyprint", "--pdf-page-size", "A4",
         "--pdf-margin", "10mm 20mm", "-f", "pdf"],
        env=WIDE,
    )
    assert result.exit_code == EXIT_FAILURE, result.output
    options = captured["options"]
    assert options.pdf_engine == "weasyprint" and options.page_size == "A4"
    assert options.margins_mm == (10.0, 20.0, 10.0, 20.0)
    assert captured["formats"] == ["pdf"]

    captured.clear()
    result = runner.invoke(
        cli.app, ["export", "-o", str(out)],
        env={**WIDE, "BOOK_TRANSLATOR_PDF_PAGE_SIZE": "Letter",
             "BOOK_TRANSLATOR_PDF_MARGIN": "72pt"},
    )
    assert result.exit_code == EXIT_FAILURE, result.output
    options = captured["options"]
    assert options.pdf_engine == "native" and options.page_size == "Letter"
    assert options.margins_mm == pytest.approx((25.4, 25.4, 25.4, 25.4))
    assert captured["margins_from_source"] is False

    captured.clear()
    result = runner.invoke(cli.app, ["export", "-o", str(out)], env=WIDE)
    assert result.exit_code == EXIT_FAILURE, result.output
    options = captured["options"]
    assert options.page_size == "source" and captured["margins_from_source"] is True
    assert options.page_rect_pt is None and options.margins_pt is None  # filled at export time
    assert captured["formats"] is None  # default rule decided by the orchestrator
    assert captured["figure_legend"] is False
    assert (captured["overlay_min_scale"], captured["overlay_floor_scale"]) == (0.65, 0.5)


# --------------------------------------------------------------------------- #
# Fail-fast settings
# --------------------------------------------------------------------------- #


def test_missing_deepl_key_fails_before_extraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: List[str] = []

    def spy_extractor(name: str) -> Any:
        calls.append(f"factory:{name}")
        raise AssertionError("extractor must not be resolved")

    def spy_extract(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append("extract")
        raise AssertionError("extract must not run")

    monkeypatch.setattr(orchestrator_module, "get_extractor", spy_extractor)
    monkeypatch.setattr(cli.Orchestrator, "extract", spy_extract)
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    result = runner.invoke(cli.app, ["run", "-i", str(pdf), "-o", str(tmp_path / "out")])

    assert result.exit_code == EXIT_FAILURE
    assert "DEEPL_API_KEY" in result.output
    assert calls == []


def test_missing_key_json_error_payload(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    result = runner.invoke(
        cli.app, ["translate", "-i", str(pdf), "-o", str(tmp_path / "out"), "--json"]
    )
    assert result.exit_code == EXIT_FAILURE
    payload = json.loads(result.stdout)
    assert payload["ok"] is False and payload["exit_code"] == 1
    assert payload["error"]["code"] == "provider_config"


def test_invalid_setting_value_is_exit_1(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    result = runner.invoke(
        cli.app,
        ["translate", "-i", str(pdf), "-o", str(tmp_path / "out"),
         "--chunk-min", "1500", "--chunk-max", "1000"],
        env={"DEEPL_API_KEY": "abc"},
    )
    assert result.exit_code == EXIT_FAILURE
    assert "CHUNK_MIN" in result.output


# --------------------------------------------------------------------------- #
# Result -> exit code through the command layer
# --------------------------------------------------------------------------- #


def _summary(exit_code: int) -> JobSummary:
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    return JobSummary(
        job_id="job", run_id="run", command="run", input_path="in.pdf", input_sha256="a" * 64,
        source_md_sha256=None, provider="deepl", glossary_strategy=None,
        glossary_entries_applied=0, extractor="pymupdf", fallback_used=False,
        counts=StatusCounts(1, 0, 2, 1, 4, 0), failed_chunk_ids=[3], review_chunk_ids=[],
        chars_sent_this_run=10, chars_sent_total=10, started_at=now, finished_at=now,
        duration_s=0.0, outputs={"markdown": "x.md"}, exit_code=exit_code, warnings=["w"],
    )


class StubOrchestrator:
    result: Any = Ok(_summary(EXIT_SUCCESS))
    received: dict = {}
    last_job_id: Any = None

    def __init__(self, settings: Any, output_dir: Path, *, tool_version: str, run_id: str) -> None:
        self.cancel = asyncio.Event()
        self.abort = asyncio.Event()
        self._progress = None
        StubOrchestrator.received = {"settings": settings, "output_dir": output_dir}

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        StubOrchestrator.received["run_kwargs"] = kwargs
        return StubOrchestrator.result

    async def translate(self, *args: Any, **kwargs: Any) -> Any:
        StubOrchestrator.received["translate_kwargs"] = kwargs
        return StubOrchestrator.result


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (Ok(_summary(EXIT_SUCCESS)), EXIT_SUCCESS),
        (Ok(_summary(EXIT_PARTIAL)), EXIT_PARTIAL),
        (Ok(_summary(EXIT_PAUSED)), EXIT_PAUSED),
        (Ok(_summary(EXIT_INTERRUPTED)), EXIT_INTERRUPTED),
        (Err(AppError(ErrorCode.STATE_LOCKED, "locked", ErrorScope.USER)), EXIT_LOCKED),
        (Err(AppError(ErrorCode.STATE_HASH_MISMATCH, "hash", ErrorScope.USER)), EXIT_MISMATCH),
        (Err(AppError(ErrorCode.INTERRUPTED, "ctrl-c", ErrorScope.USER)), EXIT_INTERRUPTED),
        (Err(AppError(ErrorCode.INTERNAL, "boom", ErrorScope.JOB_FATAL)), EXIT_FAILURE),
    ],
)
def test_run_maps_results_to_exit_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, result: Any, expected: int
) -> None:
    monkeypatch.setattr(cli, "Orchestrator", StubOrchestrator)
    StubOrchestrator.result = result
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    out = tmp_path / "out"

    invoked = runner.invoke(
        cli.app,
        ["run", "-i", str(pdf), "-o", str(out), "--json", "--concurrency", "2", "-f", "md"],
        env={"DEEPL_API_KEY": "abc"},
    )

    assert invoked.exit_code == expected, invoked.output
    payload = json.loads(invoked.stdout)
    assert payload["exit_code"] == expected
    assert payload["ok"] is (expected == EXIT_SUCCESS)
    assert StubOrchestrator.received["settings"].concurrency == 2
    assert StubOrchestrator.received["run_kwargs"]["formats"] == ["md"]
    assert (out / "logs").is_dir()  # JSONL log file created under <output>/logs


def test_run_human_summary_mentions_failed_chunks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "Orchestrator", StubOrchestrator)
    StubOrchestrator.result = Ok(_summary(EXIT_PARTIAL))
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    invoked = runner.invoke(
        cli.app, ["run", "-i", str(pdf), "-o", str(tmp_path / "out")], env={"DEEPL_API_KEY": "abc"}
    )
    assert invoked.exit_code == EXIT_PARTIAL
    assert "failed chunk ids: [3]" in invoked.stdout
    assert "exit code: 2" in invoked.stdout
    assert "\x1b[" not in invoked.stdout  # plain output, no ANSI codes


# --------------------------------------------------------------------------- #
# status and listings
# --------------------------------------------------------------------------- #


def test_status_json_on_empty_directory(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["status", "-o", str(tmp_path), "--json"])
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(result.stdout)
    assert payload["exists"] is False and payload["job_id"] is None
    assert payload["state_path"].endswith("translation_state.db")
    assert not (tmp_path / "translation_state.db").exists()


def test_status_human_on_empty_directory(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["status", "-o", str(tmp_path)])
    assert result.exit_code == EXIT_SUCCESS
    assert "no translation state" in result.stdout


def test_providers_listing() -> None:
    result = runner.invoke(cli.app, ["providers", "--json"])
    assert result.exit_code == EXIT_SUCCESS, result.output
    rows = json.loads(result.stdout)
    names = {row["name"] for row in rows}
    assert {"deepl", "local"} <= names
    deepl = next(row for row in rows if row["name"] == "deepl")
    assert deepl["available"] is False and "DEEPL_API_KEY" in deepl["detail"]

    human = runner.invoke(cli.app, ["providers"], env=WIDE)
    assert human.exit_code == EXIT_SUCCESS and "deepl" in human.output


def test_extractors_and_exporters_listings() -> None:
    extractors = runner.invoke(cli.app, ["extractors", "--json"])
    assert extractors.exit_code == EXIT_SUCCESS, extractors.output
    rows = {row["name"]: row for row in json.loads(extractors.stdout)}
    assert rows["pymupdf"]["available"] is True
    assert "marker" in rows

    exporters = runner.invoke(cli.app, ["exporters", "--json"])
    assert exporters.exit_code == EXIT_SUCCESS, exporters.output
    rows = {row["name"]: row for row in json.loads(exporters.stdout)}
    assert rows["markdown"]["available"] is True and rows["epub"]["available"] is True
    assert rows["pdf"]["available"] is True  # native engine (design doc 06, D10)
    assert rows["pdf"]["engines"]["native"] is True
    assert set(rows["pdf"]["engines"]) == {"native", "weasyprint"}
    assert "engines" not in rows["markdown"]

    human = runner.invoke(cli.app, ["exporters"], env=WIDE)
    assert human.exit_code == EXIT_SUCCESS and "native (available)" in human.output
    assert "weasyprint (" in human.output


# --------------------------------------------------------------------------- #
# Fix round (docs/04_code_review.md)
# --------------------------------------------------------------------------- #


def test_whitespace_key_is_never_echoed(tmp_path: Path) -> None:
    """CR-01: an invalid DEEPL_API_KEY is named, but its value never reaches any output."""
    secret = "abc def:fx"
    loaded = load_settings(deepl_api_key=secret)
    assert isinstance(loaded, Err)
    assert "DEEPL_API_KEY" in loaded.error.message and "whitespace" in loaded.error.message
    assert "abc def" not in str(loaded) and "def:fx" not in str(loaded)

    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    args = ["translate", "-i", str(pdf), "-o", str(tmp_path / "out"), "--dry-run"]
    human = runner.invoke(cli.app, args, env={"DEEPL_API_KEY": secret})
    assert human.exit_code == EXIT_FAILURE
    assert "DEEPL_API_KEY" in human.output
    assert "abc def" not in human.output and "def:fx" not in human.output

    machine = runner.invoke(cli.app, [*args, "--json"], env={"DEEPL_API_KEY": secret})
    assert machine.exit_code == EXIT_FAILURE
    assert "abc def" not in machine.output and "def:fx" not in machine.output
    payload = json.loads(machine.stdout)
    assert payload["error"]["code"] == "provider_config"
    assert "abc def" not in json.dumps(payload)


def test_export_before_translate_refuses_cleanly(tmp_path: Path) -> None:
    """CR-02: export on a freshly extracted directory -> exit 2, no artifact, no traceback."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "source_book.md").write_text("# Title\n\nSome text.\n", encoding="utf-8")
    db = unwrap(open_database(out / "translation_state.db", tool_version=cli.__version__))
    try:
        jobs = JobRepository(db)
        job = unwrap(
            jobs.get_or_create_job(JobSpec(str(tmp_path / "book.pdf"), "a" * 64, cli.__version__))
        )
        unwrap(jobs.update_job(job.id, JobPatch(status="EXTRACTED")))
    finally:
        close_database(db)

    result = runner.invoke(cli.app, ["export", "-o", str(out)], env=WIDE)

    assert result.exit_code == EXIT_PARTIAL, result.output
    assert "run `translate` first" in result.output
    assert "Traceback" not in result.output and "ParserError" not in result.output
    assert not (out / "translated_book.md").exists()
    assert not (out / "translated_book.epub").exists()


class StageStub(StubOrchestrator):
    """Stub for the synchronous commands (extract / glossary / export)."""

    stage_result: Any = None
    raise_interrupt: bool = False

    def _stage(self) -> Any:
        if StageStub.raise_interrupt:
            raise KeyboardInterrupt
        return StageStub.stage_result

    def extract(self, *args: Any, **kwargs: Any) -> Any:
        return self._stage()

    def glossary(self, *args: Any, **kwargs: Any) -> Any:
        return self._stage()

    def export(self, *args: Any, **kwargs: Any) -> Any:
        return self._stage()


def test_error_messages_keep_bracketed_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CR-12: rich markup is escaped, so `[local]` survives in error output and listings."""
    monkeypatch.setattr(cli, "Orchestrator", StageStub)
    StageStub.raise_interrupt = False
    StageStub.stage_result = Err(
        AppError(
            ErrorCode.PROVIDER_CONFIG,
            'install the extra with pip install "book-translator[local]"',
            ErrorScope.USER,
        )
    )
    result = runner.invoke(cli.app, ["export", "-o", str(tmp_path / "out")], env=WIDE)
    assert result.exit_code == EXIT_FAILURE
    assert "[local]" in result.output and "[provider_config]" in result.output

    listing = runner.invoke(cli.app, ["providers", "--json"])
    rows = {row["name"]: row for row in json.loads(listing.stdout)}
    if "[local]" in rows["local"]["detail"]:  # the [local] extra is not installed here
        human = runner.invoke(cli.app, ["providers"], env=WIDE)
        assert "[local]" in human.output


def test_command_failure_is_logged_to_jsonl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CR-13: a failed command leaves a `command_failed` line in logs/run-*.jsonl."""
    monkeypatch.setattr(cli, "Orchestrator", StageStub)
    StageStub.raise_interrupt = False
    StageStub.stage_result = Err(
        AppError(ErrorCode.EXPORT_FAILED, "EPUB build failed: ParserError", ErrorScope.JOB_FATAL)
    )
    out = tmp_path / "out"
    result = runner.invoke(cli.app, ["export", "-o", str(out)])
    assert result.exit_code == EXIT_FAILURE
    assert result.output.count("EPUB build failed") == 1  # printed once, not echoed by the log
    log_files = list((out / "logs").glob("run-*.jsonl"))
    assert len(log_files) == 1
    lines = [
        json.loads(line) for line in log_files[0].read_text("utf-8").splitlines() if line.strip()
    ]
    failed = [line for line in lines if line["event"] == "command_failed"]
    assert len(failed) == 1 and failed[0]["level"] == "ERROR"
    assert "export_failed" in failed[0]["message"] and "ParserError" in failed[0]["message"]
    assert failed[0]["job_id"] is None  # no job was ever bound


def test_command_failure_log_carries_the_job_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CR-37: once a job was bound, `command_failed` keeps its id after the stage unbound it."""
    monkeypatch.setattr(cli, "Orchestrator", StageStub)
    monkeypatch.setattr(StageStub, "last_job_id", "job-cr37")
    StageStub.raise_interrupt = False
    StageStub.stage_result = Err(
        AppError(ErrorCode.EXPORT_FAILED, "EPUB build failed: ParserError", ErrorScope.JOB_FATAL)
    )
    out = tmp_path / "out"
    result = runner.invoke(cli.app, ["export", "-o", str(out)])
    assert result.exit_code == EXIT_FAILURE
    log_file = next((out / "logs").glob("run-*.jsonl"))
    lines = [
        json.loads(line) for line in log_file.read_text("utf-8").splitlines() if line.strip()
    ]
    failed = [line for line in lines if line["event"] == "command_failed"]
    assert len(failed) == 1 and failed[0]["job_id"] == "job-cr37"


@pytest.mark.parametrize("command", ["extract", "glossary", "export"])
def test_ctrl_c_in_sync_commands_exits_130(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    """CR-14: Ctrl+C in a synchronous stage maps to exit 130, not click's "Aborted!" (1)."""
    monkeypatch.setattr(cli, "Orchestrator", StageStub)
    StageStub.raise_interrupt = True
    try:
        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        args = [command, "-o", str(tmp_path / "out")]
        if command == "extract":
            args += ["-i", str(pdf)]
        result = runner.invoke(cli.app, args)
    finally:
        StageStub.raise_interrupt = False
    assert result.exit_code == EXIT_INTERRUPTED, result.output
    assert "Aborted" not in result.output and "interrupted" in result.output


# --------------------------------------------------------------------------- #
# v1.1 F1 options (design doc 06, §7.1)
# --------------------------------------------------------------------------- #

FIGURE_OPTIONS = ("--figure-dpi", "--no-figures", "--figure-legend", "--no-figure-legend",
                  "--figure-legend-limit", "--figure-exclude-pages", "--figure-max-mb")


@pytest.mark.parametrize("command", ["extract", "run"])
def test_figure_options_in_help_with_defaults(command: str) -> None:
    result = runner.invoke(cli.app, [command, "--help"], env=WIDE)
    assert result.exit_code == 0, result.output
    text = result.output
    # CR-78: rendering the legend list is an export decision; `extract` no longer takes it
    export_only = ("--figure-legend", "--no-figure-legend") if command == "extract" else ()
    for option in FIGURE_OPTIONS:
        if option not in export_only:
            assert option in text, option
    assert "BOOK_TRANSLATOR_FIGURE_DPI" in text
    assert "200" in text and "40" in text and "2,5-7" in text
    assert "--allow-partial" not in text or command == "run"


def test_figure_exclude_pages_parse_error_is_exit_1(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    result = runner.invoke(
        cli.app,
        ["extract", "-i", str(pdf), "-o", str(tmp_path / "out"),
         "--figure-exclude-pages", "2,x-7"],
    )
    assert result.exit_code == EXIT_FAILURE
    assert "page range" in result.output
    assert not (tmp_path / "out" / "translation_state.db").exists()


def test_figure_dpi_out_of_range_is_rejected(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    result = runner.invoke(
        cli.app, ["extract", "-i", str(pdf), "-o", str(tmp_path / "out"), "--figure-dpi", "10"]
    )
    assert result.exit_code != 0
    assert "72" in result.output


def test_figure_flags_reach_the_orchestrator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "Orchestrator", StubOrchestrator)
    StubOrchestrator.result = Ok(_summary(EXIT_SUCCESS))
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    invoked = runner.invoke(
        cli.app,
        ["run", "-i", str(pdf), "-o", str(tmp_path / "out"), "--json", "--figure-dpi", "150",
         "--no-figure-legend", "--figure-legend-limit", "7", "--figure-exclude-pages", "2,5-7",
         "--figure-max-mb", "0.5"],
        env={"DEEPL_API_KEY": "abc"},
    )
    assert invoked.exit_code == EXIT_SUCCESS, invoked.output
    options = StubOrchestrator.received["run_kwargs"]["figure_options"]
    # Addendum A: the legend block is always extracted (translation vehicle); the flag only
    # decides whether the list is rendered in the outputs.
    assert options.enabled and options.dpi == 150 and options.legend is True
    assert StubOrchestrator.received["run_kwargs"]["figure_legend"] is False
    assert options.legend_limit == 7 and options.exclude_pages == frozenset({2, 5, 6, 7})
    assert options.max_bytes == 512 * 1024
    settings = StubOrchestrator.received["settings"]
    assert settings.figure_dpi == 150 and settings.figure_min_paths == 5

    invoked = runner.invoke(
        cli.app, ["run", "-i", str(pdf), "-o", str(tmp_path / "out"), "--json", "--no-figures"],
        env={"DEEPL_API_KEY": "abc", "BOOK_TRANSLATOR_FIGURE_MIN_PATHS": "9"},
    )
    assert invoked.exit_code == EXIT_SUCCESS, invoked.output
    options = StubOrchestrator.received["run_kwargs"]["figure_options"]
    assert options.enabled is False and options.min_paths == 9


def test_figure_line_in_extract_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from book_translator.domain.models import FigureTotals
    from book_translator.pipeline.orchestrator import ExtractOutcome

    totals = FigureTotals(3, 1, 2, 0, 412 * 1024, "p002-f01.png", 300_000, 2, ((5, "excluded"),), 1)
    outcome = ExtractOutcome(
        job_id="job", source_path=tmp_path / "source_book.md", extractor="pymupdf",
        fallback_used=False, pages=3, skipped_pages=0, detected_language="en",
        language_confidence=0.4, image_count=3, warnings=[], figures=totals,
    )

    class ExtractStub(StubOrchestrator):
        def extract(self, *args: Any, **kwargs: Any) -> Any:
            return Ok(outcome)

    monkeypatch.setattr(cli, "Orchestrator", ExtractStub)
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    invoked = runner.invoke(cli.app, ["extract", "-i", str(pdf), "-o", str(tmp_path / "out")])
    assert invoked.exit_code == EXIT_SUCCESS, invoked.output
    assert "figures: 3 (vector 2, raster 1, 412 KB, largest p002-f01.png" in invoked.output
    assert "dpi reduced for 1" in invoked.output and "1 pages skipped" in invoked.output
    invoked = runner.invoke(
        cli.app, ["extract", "-i", str(pdf), "-o", str(tmp_path / "out"), "--json"]
    )
    payload = json.loads(invoked.stdout)
    assert payload["figures"]["count"] == 3
    assert payload["figures"]["pages_skipped"] == [[5, "excluded"]]


# --------------------------------------------------------------------------- #
# v1.1 F2 / Addendum A: --mode, pdf-overlay, overlay options, "source" defaults
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("command", ["run", "translate"])
def test_mode_and_overlay_translate_options_in_help(command: str) -> None:
    result = runner.invoke(cli.app, [command, "--help"], env=WIDE)
    assert result.exit_code == 0, result.output
    text = result.output
    for option in ("--mode", "--overlay-translate-headers", "--overlay-keep-figure-text"):
        assert option in text, option
    assert "reflow" in text and "overlay" in text and "BOOK_TRANSLATOR_MODE" in text


@pytest.mark.parametrize("command", ["run", "export"])
def test_overlay_export_options_in_help(command: str) -> None:
    result = runner.invoke(cli.app, [command, "--help"], env=WIDE)
    assert result.exit_code == 0, result.output
    text = result.output
    for option in ("--overlay-min-scale", "--overlay-floor-scale", "--figure-legend",
                   "--pdf-page-size", "--pdf-margin"):
        assert option in text, option
    assert "pdf-overlay" in text and "0.65" in text and "0.5" in text
    assert "--no-figure-legend" in text  # the legend list is off by default
    assert "A5" not in text and "18mm" not in text


def test_settings_defaults_follow_the_source() -> None:
    settings = unwrap(load_settings())
    assert settings.pdf_page_size == "source" and settings.pdf_margin == "source"
    assert settings.figure_legend is False
    assert (settings.overlay_min_scale, settings.overlay_floor_scale) == (0.65, 0.5)
    assert (settings.overlay_batch_min_chars, settings.overlay_context_chars) == (1000, 4000)


def test_run_forwards_mode_formats_and_overlay_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "Orchestrator", StubOrchestrator)
    StubOrchestrator.result = Ok(_summary(EXIT_SUCCESS))
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    base = ["run", "-i", str(pdf), "-o", str(tmp_path / "out"), "--json"]

    invoked = runner.invoke(cli.app, base, env={"DEEPL_API_KEY": "abc"})
    assert invoked.exit_code == EXIT_SUCCESS, invoked.output
    kwargs = StubOrchestrator.received["run_kwargs"]
    assert kwargs["mode"] == "reflow" and kwargs["formats"] is None  # default rule downstream
    assert kwargs["export_options"].page_size == "source"
    assert kwargs["margins_from_source"] is True and kwargs["figure_legend"] is False
    assert not kwargs["overlay_options"].translate_headers

    invoked = runner.invoke(
        cli.app,
        [*base, "--mode", "overlay", "--overlay-translate-headers", "--overlay-keep-figure-text",
         "--overlay-min-scale", "0.8", "--overlay-floor-scale", "0.6", "-f", "pdf-overlay",
         "--figure-legend", "--pdf-page-size", "A4", "--pdf-margin", "10mm"],
        env={"DEEPL_API_KEY": "abc"},
    )
    assert invoked.exit_code == EXIT_SUCCESS, invoked.output
    kwargs = StubOrchestrator.received["run_kwargs"]
    assert kwargs["mode"] == "overlay" and kwargs["formats"] == ["pdf-overlay"]
    assert kwargs["overlay_options"].translate_headers
    assert kwargs["overlay_options"].keep_figure_text
    assert (kwargs["overlay_min_scale"], kwargs["overlay_floor_scale"]) == (0.8, 0.6)
    assert kwargs["figure_legend"] is True and kwargs["margins_from_source"] is False
    assert kwargs["export_options"].page_size == "A4"
    assert kwargs["export_options"].margins_mm == (10.0, 10.0, 10.0, 10.0)


def test_translate_forwards_the_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "Orchestrator", StubOrchestrator)
    StubOrchestrator.result = Ok(_summary(EXIT_SUCCESS))
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    invoked = runner.invoke(
        cli.app,
        ["translate", "-i", str(pdf), "-o", str(tmp_path / "out"), "--json", "--mode", "overlay",
         "--overlay-keep-figure-text", "--dry-run"],
        env={"DEEPL_API_KEY": "abc"},
    )
    assert invoked.exit_code == EXIT_SUCCESS, invoked.output
    kwargs = StubOrchestrator.received["translate_kwargs"]
    assert kwargs["mode"] == "overlay" and kwargs["dry_run"] is True
    assert kwargs["overlay_options"].keep_figure_text
    assert not kwargs["overlay_options"].translate_headers

    usage = runner.invoke(
        cli.app, ["translate", "-i", str(pdf), "-o", str(tmp_path / "out"), "--mode", "inplace"],
        env={"DEEPL_API_KEY": "abc"},
    )
    assert usage.exit_code == 2 and "inplace" in usage.output


def test_overlay_floor_above_min_scale_is_a_user_error(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    bad = runner.invoke(
        cli.app,
        ["export", "-o", str(out), "--overlay-min-scale", "0.5", "--overlay-floor-scale", "0.7",
         "--json"],
        env=WIDE,
    )
    assert bad.exit_code == EXIT_FAILURE, bad.output
    payload = json.loads(bad.stdout)
    assert payload["error"]["scope"] == "user"
    assert "--overlay-floor-scale" in payload["error"]["message"]
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    bad_run = runner.invoke(
        cli.app,
        ["run", "-i", str(pdf), "-o", str(out), "--overlay-min-scale", "0.5",
         "--overlay-floor-scale", "0.7", "--json"],
        env={**WIDE, "DEEPL_API_KEY": "abc"},
    )
    assert bad_run.exit_code == EXIT_FAILURE, bad_run.output


def test_export_pdf_overlay_on_an_empty_directory_refuses_cleanly(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    result = runner.invoke(cli.app, ["export", "-o", str(out), "-f", "pdf-overlay", "--json"],
                           env=WIDE)
    assert result.exit_code == EXIT_FAILURE, result.output
    assert json.loads(result.stdout)["error"]["code"] == "state_not_found"
    assert not (out / "translated_book.overlay.pdf").exists()


def test_exporters_listing_names_pdf_overlay() -> None:
    result = runner.invoke(cli.app, ["exporters", "--json"], env=WIDE)
    assert result.exit_code == 0, result.output
    rows = {row["name"]: row for row in json.loads(result.stdout)}
    assert "pdf-overlay" in rows and ".overlay.pdf" in rows["pdf-overlay"]["detail"]
    assert {"markdown", "epub", "pdf"} <= set(rows)


def test_overlay_summary_line_in_human_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import dataclasses

    from book_translator.domain.models import OverlaySummary

    overlay = OverlaySummary(
        status="OVERLAY_TRANSLATED", counts=StatusCounts(0, 0, 31, 0, 31, 0), pages_completed=3,
        pages_total=3, pages_skipped=1, translatable_units=22, kept_units=9,
        overlay_sha256="a" * 64, failed_unit_ids=[], review_unit_ids=[4, 9],
    )
    summary = dataclasses.replace(_summary(EXIT_SUCCESS), mode="overlay", overlay=overlay)
    monkeypatch.setattr(cli, "Orchestrator", StubOrchestrator)
    StubOrchestrator.result = Ok(summary)
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    invoked = runner.invoke(
        cli.app, ["translate", "-i", str(pdf), "-o", str(tmp_path / "out"), "--mode", "overlay"],
        env={"DEEPL_API_KEY": "abc"},
    )
    assert invoked.exit_code == EXIT_SUCCESS, invoked.output
    assert "mode overlay" in invoked.output
    assert "overlay: 31/31 units, 3/3 pages, status OVERLAY_TRANSLATED" in invoked.output
    assert "1 pages without text layer" in invoked.output and "review 2" in invoked.output


# --------------------------------------------------------------------------- #
# v1.1 fix round Q: export --input, uniform scale, help drift, exit codes (CR-39/51/78, Q21)
# --------------------------------------------------------------------------- #


def test_export_forwards_input_and_uniform_scale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: Dict[str, Any] = {}

    def fake_export(self: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return Err(AppError(ErrorCode.STATE_NOT_FOUND, "no state", ErrorScope.USER))

    monkeypatch.setattr(cli.Orchestrator, "export", fake_export)
    out = tmp_path / "out"
    out.mkdir()
    moved = tmp_path / "moved book.pdf"
    result = runner.invoke(
        cli.app,
        ["export", "-o", str(out), "--input", str(moved), "--no-overlay-uniform-scale"],
        env=WIDE,
    )
    assert result.exit_code == EXIT_FAILURE, result.output
    assert captured["input_pdf"] == moved and captured["overlay_uniform_scale"] is False

    captured.clear()
    runner.invoke(cli.app, ["export", "-o", str(out)], env=WIDE)
    assert captured["input_pdf"] is None and captured["overlay_uniform_scale"] is True


def test_run_forwards_the_uniform_scale_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "Orchestrator", StubOrchestrator)
    StubOrchestrator.result = Ok(_summary(EXIT_SUCCESS))
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    base = ["run", "-i", str(pdf), "-o", str(tmp_path / "out"), "--mode", "overlay"]
    runner.invoke(cli.app, base, env={"DEEPL_API_KEY": "abc"})
    assert StubOrchestrator.received["run_kwargs"]["overlay_uniform_scale"] is True  # default on
    runner.invoke(cli.app, [*base, "--no-overlay-uniform-scale"], env={"DEEPL_API_KEY": "abc"})
    assert StubOrchestrator.received["run_kwargs"]["overlay_uniform_scale"] is False


def test_help_texts_after_the_review(tmp_path: Path) -> None:
    """CR-78: `extract --figure-legend` had no effect; rich markup ate `[pdf]`."""
    extract_help = runner.invoke(cli.app, ["extract", "--help"], env=WIDE).output
    assert "--figure-legend-limit" in extract_help
    assert "--figure-legend " not in extract_help and "--no-figure-legend" not in extract_help
    export_help = runner.invoke(cli.app, ["export", "--help"], env=WIDE).output
    assert "([pdf] extra)" in export_help and "( extra)" not in export_help
    assert "--input" in export_help and "--overlay-uniform-scale" in export_help
    assert "--no-overlay-uniform-scale" in export_help
    run_help = runner.invoke(cli.app, ["run", "--help"], env=WIDE).output
    assert "--overlay-uniform-scale" in run_help and "--figure-legend" in run_help


def _overlay_state(tmp_path: Path) -> Any:
    """A directory with a finished overlay pass (fake provider), built without the CLI."""
    from tests.conftest import FakeTranslator
    from tests.test_orchestrator import build, pdf_workspace

    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    unwrap(asyncio.run(orch.translate(ws.input_pdf, mode="overlay")))
    return ws


def test_export_of_a_swapped_source_pdf_is_exit_5_through_the_cli(tmp_path: Path) -> None:
    """CR-39 through the real CLI: different file at the recorded path, then via --input."""
    import pymupdf

    ws = _overlay_state(tmp_path)
    foreign = tmp_path / "foreign.pdf"
    doc = pymupdf.open()
    for number in range(3):
        doc.new_page().insert_text((72, 100), f"FOREIGN DOCUMENT page {number}")
    doc.save(str(foreign))
    doc.close()

    result = runner.invoke(
        cli.app, ["export", "-o", str(ws.output), "-f", "pdf-overlay", "--input", str(foreign),
                  "--json"], env=WIDE,
    )
    assert result.exit_code == EXIT_MISMATCH, result.output
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "state_hash_mismatch"
    assert not (ws.output / "translated_book.overlay.pdf").exists()
    assert not (ws.output / "overlay_review.json").exists()

    original = ws.input_pdf.read_bytes()
    ws.input_pdf.write_bytes(foreign.read_bytes())
    result = runner.invoke(cli.app, ["export", "-o", str(ws.output), "--json"], env=WIDE)
    assert result.exit_code == EXIT_MISMATCH, result.output
    assert not (ws.output / "translated_book.overlay.pdf").exists()

    ws.input_pdf.unlink()
    result = runner.invoke(cli.app, ["export", "-o", str(ws.output), "--json"], env=WIDE)
    assert result.exit_code == EXIT_FAILURE, result.output
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "input_not_found" and "export --input" in error["message"]
    ws.input_pdf.write_bytes(original)


def test_q21_exit_codes_and_the_placement_line_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import book_translator.exporters.pdf_overlay_exporter as exporter_module
    from tests.test_orchestrator import FakeOverlayRenderer

    ws = _overlay_state(tmp_path)
    FakeOverlayRenderer.calls = []
    FakeOverlayRenderer.pages_skipped = ()
    FakeOverlayRenderer.counts = {"placed": 32, "shrunk_below_threshold": 2}
    FakeOverlayRenderer.review = ((1, "p001-b004", 4, "shrunk_below_threshold", 0.6),)
    monkeypatch.setattr(exporter_module, "OverlayRenderer", FakeOverlayRenderer)

    result = runner.invoke(cli.app, ["export", "-o", str(ws.output)], env=WIDE)
    assert result.exit_code == EXIT_SUCCESS, result.output  # review list alone: exit 0
    flat = " ".join(result.output.split())
    assert "overlay placement: placed 32, shrunk below threshold 2, could not fit 0" in flat
    assert "review entries 1" in flat

    FakeOverlayRenderer.counts = {"placed": 33, "could_not_fit": 1}
    result = runner.invoke(cli.app, ["export", "-o", str(ws.output), "--json"], env=WIDE)
    assert result.exit_code == EXIT_PARTIAL, result.output
    payload = json.loads(result.stdout)
    assert payload["overlay"]["could_not_fit"] == 1 and payload["ok"] is False

    FakeOverlayRenderer.counts = {}
    FakeOverlayRenderer.pages_skipped = (2,)
    result = runner.invoke(cli.app, ["export", "-o", str(ws.output)], env=WIDE)
    assert result.exit_code == EXIT_PARTIAL, result.output
    status = runner.invoke(cli.app, ["status", "-o", str(ws.output), "--json"], env=WIDE)
    assert status.exit_code == 0, status.output


# --------------------------------------------------------------------------- #
# v1.1 fix round Q (2nd): partial reports and a changed source (CR-91, CR-92)
# --------------------------------------------------------------------------- #


def test_a_failed_format_is_printed_as_an_error_next_to_the_files_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-91: exit 1, but the md/epub that reached the disk are listed, and the format that
    failed is printed as ``error:`` rather than hidden among the warnings."""
    outcome = orchestrator_module.ExportOutcome(
        job_id="job000000001",
        outputs={"markdown": str(tmp_path / "out" / "translated_book.md"),
                 "epub": str(tmp_path / "out" / "translated_book.epub")},
        failed_chunk_ids=[],
        review_chunk_ids=[],
        warnings=["figure_orphan:extra.png",
                  "export_error:pdf:unknown PDF page size 'Foo'"],
        exit_code=EXIT_FAILURE,
    )
    monkeypatch.setattr(cli.Orchestrator, "export", lambda self, **kwargs: Ok(outcome))
    out = tmp_path / "out"
    out.mkdir()

    result = runner.invoke(cli.app, ["export", "-o", str(out)], env=WIDE)
    assert result.exit_code == EXIT_FAILURE, result.output
    flat = " ".join(result.output.split())
    assert "output markdown:" in flat and "output epub:" in flat
    assert "error: export_error:pdf:unknown PDF page size 'Foo'" in flat
    assert "warning: figure_orphan:extra.png" in flat
    assert "exit code: 1" in flat

    result = runner.invoke(cli.app, ["export", "-o", str(out), "--json"], env=WIDE)
    assert result.exit_code == EXIT_FAILURE, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False and sorted(payload["outputs"]) == ["epub", "markdown"]
    assert any(w.startswith("export_error:pdf:") for w in payload["warnings"])


def test_a_changed_source_pdf_still_exports_md_through_the_cli(tmp_path: Path) -> None:
    """CR-92: only md was asked for; the source PDF is needed for the figure labels alone."""
    from tests.test_orchestrator import _translated_figure_workspace

    ws, _ = asyncio.run(_translated_figure_workspace(tmp_path))
    png = (ws.output / "images" / "p002-f01.png").read_bytes()
    ws.input_pdf.write_bytes(ws.input_pdf.read_bytes() + b"% re-saved\n")  # same book, new bytes

    result = runner.invoke(
        cli.app, ["export", "-o", str(ws.output), "-f", "md", "--json"], env=WIDE
    )
    assert result.exit_code == EXIT_PARTIAL, result.output
    payload = json.loads(result.stdout)
    assert list(payload["outputs"]) == ["markdown"]
    assert any(
        w.startswith("figure_translate_failed:input_changed:") for w in payload["warnings"]
    ), payload["warnings"]
    markdown = (ws.output / "translated_book.md").read_text("utf-8")
    assert "2D (what?) — TR:2D (what?)" in markdown  # the paid label translations survive
    assert (ws.output / "images" / "p002-f01.png").read_bytes() == png  # never re-rendered
    assert not (ws.output / "images" / "p002-f01.tr.png").exists()


# --------------------------------------------------------------------------- #
# --fallback-provider (automatic switch on an exhausted quota)
# --------------------------------------------------------------------------- #


GOOGLE_KEY = "AIzaSyD-FAKE-key-for-tests-0123456789"


def test_google_is_listed_as_a_provider() -> None:
    rows = json.loads(runner.invoke(cli.app, ["providers", "--json"]).stdout)
    google = next(row for row in rows if row["name"] == "google")
    assert google["available"] is False and "GOOGLE_CLOUD_API" in google["detail"]

    with_key = json.loads(
        runner.invoke(
            cli.app, ["providers", "--json"], env={"GOOGLE_CLOUD_API": GOOGLE_KEY}
        ).stdout
    )
    ready = next(row for row in with_key if row["name"] == "google")
    assert ready["available"] is True
    assert "post-check only" in ready["detail"]  # v2 has no glossary
    assert GOOGLE_KEY not in json.dumps(with_key)


def test_fallback_provider_is_forwarded_to_both_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "Orchestrator", StubOrchestrator)
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    env = {"DEEPL_API_KEY": "abc", "GOOGLE_CLOUD_API": GOOGLE_KEY}

    for command, key in (("run", "run_kwargs"), ("translate", "translate_kwargs")):
        result = runner.invoke(
            cli.app,
            [command, "-i", str(pdf), "-o", str(tmp_path / "out"),
             "--fallback-provider", "google"],
            env=env,
        )
        assert result.exit_code == EXIT_SUCCESS, result.output
        assert StubOrchestrator.received[key]["fallback_provider"] == "google"


def test_fallback_provider_defaults_to_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Off by default: sending the book to a second paid API is the user's call."""
    monkeypatch.setattr(cli, "Orchestrator", StubOrchestrator)
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    result = runner.invoke(
        cli.app, ["translate", "-i", str(pdf), "-o", str(tmp_path / "out")],
        env={"DEEPL_API_KEY": "abc"},
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert StubOrchestrator.received["translate_kwargs"]["fallback_provider"] is None


def test_fallback_provider_needs_its_own_key(tmp_path: Path) -> None:
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    result = runner.invoke(
        cli.app,
        ["translate", "-i", str(pdf), "-o", str(tmp_path / "out"),
         "--fallback-provider", "google"],
        env={"DEEPL_API_KEY": "abc"},
    )

    assert result.exit_code == EXIT_FAILURE
    assert "GOOGLE_CLOUD_API" in result.output and "--fallback-provider" in result.output


def test_fallback_provider_cannot_be_the_primary(tmp_path: Path) -> None:
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    result = runner.invoke(
        cli.app,
        ["translate", "-i", str(pdf), "-o", str(tmp_path / "out"),
         "--provider", "deepl", "--fallback-provider", "deepl"],
        env={"DEEPL_API_KEY": "abc"},
    )

    assert result.exit_code == EXIT_FAILURE
    assert "also the primary provider" in result.output


def test_summary_prints_the_provider_breakdown_only_when_it_says_something() -> None:
    single = dataclasses.replace(_summary(EXIT_SUCCESS), provider_units={"deepl": 4})
    mixed = dataclasses.replace(
        _summary(EXIT_SUCCESS), provider_units={"deepl": 2, "google": 2}
    )
    common = cli._common(Path("."), None, None, True, False, False)

    with_one = _capture_summary(common, single)
    with_two = _capture_summary(common, mixed)

    assert "units by provider" not in with_one
    assert "units by provider: deepl 2, google 2" in with_two


def _capture_summary(common: Any, summary: JobSummary) -> str:
    printed: List[str] = []
    original = cli.typer.echo
    try:
        cli.typer.echo = printed.append  # type: ignore[assignment]
        cli._print_summary(common, summary)
    finally:
        cli.typer.echo = original  # type: ignore[assignment]
    return "\n".join(printed)


# --------------------------------------------------------------------------- #
# glossary --cleanup-remote (DeepL caps how many glossaries an account may hold)
# --------------------------------------------------------------------------- #


class GlossaryStoreTranslator(BaseTranslator):
    """Provider that keeps glossaries on its side; records what the cleanup deleted."""

    name = "deepl"
    capabilities = dataclasses.replace(DEFAULT_CAPABILITIES, supports_glossary=True)
    listing: List[ProviderGlossary] = []
    deleted: List[str] = []
    keep_hashes: List[Optional[str]] = []
    closed = False

    @classmethod
    def create(cls, settings: Any) -> Any:
        return Ok(cls())

    async def prepare(self, glossary: Any, run_id: str) -> Any:
        raise AssertionError("cleanup must not translate anything")

    async def translate(self, texts: Any, request: Any) -> Any:
        raise AssertionError("cleanup must not translate anything")

    async def cleanup_glossaries(self, *, keep_hash: Optional[str] = None) -> Any:
        GlossaryStoreTranslator.keep_hashes.append(keep_hash)
        return await super().cleanup_glossaries(keep_hash=keep_hash)

    async def list_provider_glossaries(self) -> Any:
        return Ok(list(GlossaryStoreTranslator.listing))

    async def delete_provider_glossary(self, glossary_id: str) -> Any:
        GlossaryStoreTranslator.deleted.append(glossary_id)
        return Ok(None)

    async def aclose(self) -> None:
        GlossaryStoreTranslator.closed = True


@pytest.fixture
def glossary_store(monkeypatch: pytest.MonkeyPatch) -> Any:
    GlossaryStoreTranslator.listing = []
    GlossaryStoreTranslator.deleted = []
    GlossaryStoreTranslator.keep_hashes = []
    GlossaryStoreTranslator.closed = False
    monkeypatch.setattr(
        orchestrator_module, "get_translator", lambda name, settings: Ok(GlossaryStoreTranslator())
    )
    return GlossaryStoreTranslator


def _extracted(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    (out / "source_book.md").write_text("# Title\n\nA short page of text.\n", encoding="utf-8")
    return out


def test_cleanup_remote_deletes_the_tools_old_glossaries_and_says_which(
    glossary_store: Any, tmp_path: Path
) -> None:
    """The option used to print "not implemented", which left the user stuck: DeepL refused
    a new glossary and the tool could not delete the old one either."""
    current = EMPTY_GLOSSARY_HASH[:16]
    glossary_store.listing = [
        ProviderGlossary("g-old", "book-translator:0123456789abcdef", 807, "0123456789abcdef"),
        ProviderGlossary("g-mine", "ai-ml terms", 12, None),
        ProviderGlossary("g-now", f"book-translator:{current}", 3, current),
    ]
    out = _extracted(tmp_path)

    result = runner.invoke(cli.app, ["glossary", "-o", str(out), "--cleanup-remote"], env=WIDE)

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert glossary_store.deleted == ["g-old"]  # only the tool's own, stale one
    assert glossary_store.keep_hashes == [EMPTY_GLOSSARY_HASH]
    assert glossary_store.closed is True
    assert "deleted 1 deepl glossary created by this tool" in result.output
    assert "book-translator:0123456789abcdef (807 entries)" in result.output
    assert f"kept book-translator:{current} (the current one)" in result.output
    assert "1 glossary not made by this tool" in result.output


def test_cleanup_remote_says_so_when_there_is_nothing_to_delete(
    glossary_store: Any, tmp_path: Path
) -> None:
    glossary_store.listing = [ProviderGlossary("g-mine", "ai-ml terms", 12, None)]
    out = _extracted(tmp_path)

    result = runner.invoke(cli.app, ["glossary", "-o", str(out), "--cleanup-remote"], env=WIDE)

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert glossary_store.deleted == []
    assert "deleted nothing; this tool has no other glossary on deepl" in result.output


def test_cleanup_remote_reports_a_provider_that_keeps_no_glossaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class NoGlossaries(GlossaryStoreTranslator):
        name = "local"
        capabilities = DEFAULT_CAPABILITIES  # supports_glossary=False

        async def list_provider_glossaries(self) -> Any:
            raise AssertionError("a provider without glossaries is never asked")

    monkeypatch.setattr(
        orchestrator_module, "get_translator", lambda name, settings: Ok(NoGlossaries())
    )
    out = _extracted(tmp_path)

    result = runner.invoke(
        cli.app,
        ["glossary", "-o", str(out), "--cleanup-remote"],
        env={**WIDE, "BOOK_TRANSLATOR_PROVIDER": "local"},
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "'local' keeps no glossaries on its side" in result.output


def test_glossary_without_the_flag_never_contacts_the_provider(
    glossary_store: Any, tmp_path: Path
) -> None:
    glossary_store.listing = [
        ProviderGlossary("g-old", "book-translator:0123456789abcdef", 807, "0123456789abcdef")
    ]
    out = _extracted(tmp_path)

    result = runner.invoke(cli.app, ["glossary", "-o", str(out)], env=WIDE)

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert glossary_store.deleted == [] and glossary_store.keep_hashes == []
    assert "cleanup-remote" not in result.output


def test_cleanup_remote_failure_fails_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Rejecting(GlossaryStoreTranslator):
        async def list_provider_glossaries(self) -> Any:
            return Err(AppError(ErrorCode.PROVIDER_AUTH, "DeepL rejected the API key",
                                ErrorScope.JOB_FATAL))

    monkeypatch.setattr(
        orchestrator_module, "get_translator", lambda name, settings: Ok(Rejecting())
    )
    out = _extracted(tmp_path)

    result = runner.invoke(cli.app, ["glossary", "-o", str(out), "--cleanup-remote"], env=WIDE)

    assert result.exit_code == EXIT_PAUSED  # provider_auth
    assert "rejected the API key" in result.output


def test_cleanup_remote_json_output_lists_the_deleted_glossaries(
    glossary_store: Any, tmp_path: Path
) -> None:
    glossary_store.listing = [
        ProviderGlossary("g-old", "book-translator:0123456789abcdef", 807, "0123456789abcdef")
    ]
    out = _extracted(tmp_path)

    result = runner.invoke(
        cli.app, ["glossary", "-o", str(out), "--cleanup-remote", "--json"], env=WIDE
    )

    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(result.stdout)
    assert payload["cleanup"]["provider"] == "deepl"
    assert payload["cleanup"]["deleted"] == [
        {"name": "book-translator:0123456789abcdef", "entries": 807}
    ]


def test_every_command_is_registered_before_the_entry_point() -> None:
    """A command defined below ``main()`` is invisible to ``python -m``.

    The module runs top to bottom: it reaches the ``__main__`` guard, calls ``main()``
    and parses argv before a decorator further down has executed. Imported as a module -
    the console script's path - the whole file runs first, so ``book-translator web``
    worked while ``python -m book_translator.cli web`` answered "No such command".
    """
    source = Path(cli.__file__).read_text(encoding="utf-8")
    guard = source.index('if __name__ == "__main__":')
    assert "@app.command()" not in source[guard:], (
        "a command is registered after the __main__ guard; move it above main()"
    )
