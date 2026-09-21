"""Local web UI: upload -> (estimate ->) translate -> download, and every refusal on the way.

No test here ever reaches a provider: the application is built with a scripted
``FakeTranslator`` / ``IdentityTranslator`` from ``conftest``. The estimate test asserts
that the provider is still untouched while the browser is looking at the character
count - that gate is the whole point of the dry run.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pymupdf
import pytest

pytest.importorskip("fastapi", reason='the web extra is optional: pip install -e ".[web]"')

from fastapi.testclient import TestClient  # noqa: E402

from book_translator.domain.result import AppError, ErrorCode, ErrorScope  # noqa: E402
from book_translator.translators.base import BaseTranslator  # noqa: E402
from book_translator.web import jobs as jobs_module  # noqa: E402
from book_translator.web.app import create_app, safe_filename  # noqa: E402

from .conftest import (  # noqa: E402
    FakeTranslator,
    IdentityTranslator,
    translator_factory,
)

API_KEY = "0123abcd-4567-89ef-0123-456789abcdef:fx"
"""A DeepL-shaped key: it must never come back out of the API."""


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No stray ``.env``, no inherited settings, a key that is only ever faked."""
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("BOOK_TRANSLATOR_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DEEPL_SERVER_URL", raising=False)
    monkeypatch.setenv("DEEPL_API_KEY", API_KEY)


def make_client(
    root: Path,
    *,
    translator: Optional[BaseTranslator] = None,
    runner: jobs_module.JobRunner = jobs_module.inline_runner,
    glossary_dir: Optional[Path] = None,
    max_upload_bytes: Optional[int] = None,
) -> TestClient:
    app = create_app(
        work_root=root / "web-jobs",
        glossary_dir=glossary_dir if glossary_dir is not None else root / "glossaries",
        translator_factory=translator_factory(translator or IdentityTranslator()),
        runner=runner,
        configure_logging=False,
        max_upload_bytes=max_upload_bytes,
    )
    return TestClient(app)


def upload(
    client: TestClient,
    pdf: Path,
    *,
    name: str = "book.pdf",
    mode: str = "reflow",
    glossary: str = "",
    estimate: bool = False,
) -> Any:
    return client.post(
        "/api/jobs",
        files={"file": (name, pdf.read_bytes(), "application/pdf")},
        data={"mode": mode, "glossary": glossary, "estimate": "true" if estimate else "false"},
    )


def blank_pdf(path: Path) -> Path:
    """One empty page: no text layer at all (``NO_TEXT_LAYER``)."""
    doc = pymupdf.open()
    doc.new_page(width=400, height=600)
    doc.save(str(path))
    doc.close()
    return path


def poll_until(
    client: TestClient, job_id: str, done: Callable[[Dict[str, Any]], bool], timeout: float = 60.0
) -> List[Dict[str, Any]]:
    """Poll the status endpoint like the browser does; return every snapshot seen."""
    seen: List[Dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/jobs/{job_id}").json()
        seen.append(snapshot)
        if done(snapshot):
            return seen
        time.sleep(0.01)
    raise AssertionError(f"job did not finish in {timeout}s; last: {seen[-1] if seen else None}")


def terminal(snapshot: Dict[str, Any]) -> bool:
    return snapshot["phase"] in {"done", "error", "awaiting_confirm"}


def write_glossary(directory: Path, name: str, source: str, target: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "source_lang": "en",
                "target_lang": "tr",
                "entries": [{"source": source, "target": target}],
            }
        ),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_index_page_is_served(tmp_path: Path) -> None:
    response = make_client(tmp_path).get("/")
    assert response.status_code == 200
    assert "book-translator" in response.text
    assert "text/html" in response.headers["content-type"]
    # offline tool: no external stylesheet or script may be referenced
    assert "http://" not in response.text and "https://" not in response.text


def test_upload_starts_a_job_and_returns_an_id(
    tmp_path: Path, overlay_pdfs: Dict[str, Path]
) -> None:
    client = make_client(tmp_path)
    response = upload(client, overlay_pdfs["overlay_styles"])
    assert response.status_code == 201
    body = response.json()
    assert body["id"]
    assert body["filename"] == "book.pdf"
    assert body["mode"] == "reflow"
    # the inline runner finished the pipeline before the response was written
    assert body["phase"] == "done", body
    assert client.get(f"/api/jobs/{body['id']}").json()["id"] == body["id"]


def test_finished_job_exposes_the_outputs_and_the_character_counter(
    tmp_path: Path, overlay_pdfs: Dict[str, Path]
) -> None:
    client = make_client(tmp_path)
    job = upload(client, overlay_pdfs["overlay_styles"]).json()
    result = job["result"]
    assert result["exit_code"] == 0
    assert result["chars_sent_this_run"] > 0
    assert result["chars_sent_total"] == result["chars_sent_this_run"]
    keys = {output["key"] for output in result["outputs"]}
    assert {"markdown", "epub"} <= keys
    for output in result["outputs"]:
        assert output["label"] and output["size_bytes"] > 0


def test_overlay_mode_produces_the_overlay_pdf(
    tmp_path: Path, overlay_pdfs: Dict[str, Path]
) -> None:
    client = make_client(tmp_path)
    job = upload(client, overlay_pdfs["overlay_styles"], mode="overlay").json()
    assert job["phase"] == "done", job
    outputs = {o["key"]: o["name"] for o in job["result"]["outputs"]}
    assert outputs == {"pdf-overlay": "translated_book.overlay.pdf"}


def test_download_returns_the_produced_file(
    tmp_path: Path, overlay_pdfs: Dict[str, Path]
) -> None:
    client = make_client(tmp_path)
    job = upload(client, overlay_pdfs["overlay_styles"]).json()
    response = client.get(f"/api/jobs/{job['id']}/download/markdown")
    assert response.status_code == 200
    on_disk = tmp_path / "web-jobs" / job["id"] / "translated_book.md"
    assert response.content == on_disk.read_bytes()
    assert "translated_book.md" in response.headers.get("content-disposition", "")


def test_download_refuses_a_key_that_is_not_an_output(
    tmp_path: Path, overlay_pdfs: Dict[str, Path]
) -> None:
    client = make_client(tmp_path)
    job = upload(client, overlay_pdfs["overlay_styles"]).json()
    for key in ("pdf", "../../../../etc/passwd", "translation_state.db"):
        response = client.get(f"/api/jobs/{job['id']}/download/{key}")
        assert response.status_code == 404, key
    assert client.get("/api/jobs/does-not-exist").status_code == 404


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #


def test_status_endpoint_reports_the_stage_and_the_progress(
    tmp_path: Path, ten_unit_pdf: Path
) -> None:
    """A real background run: the browser sees a stage while it works and the unit
    counters afterwards."""
    translator = FakeTranslator(latency=0.25)
    client = make_client(tmp_path, translator=translator, runner=jobs_module.thread_runner)
    job = upload(client, ten_unit_pdf, mode="overlay").json()
    assert job["phase"] == "running"
    assert job["stages"] == ["extract", "glossary", "translate", "export"]

    seen = poll_until(client, job["id"], terminal)
    stages = [s["stage"] for s in seen if s["stage"] is not None]
    assert stages, "no stage was ever reported"
    assert set(stages) <= {"extract", "glossary", "translate", "export"}
    assert "translate" in stages
    labelled = [s["stage_label"] for s in seen if s["stage"] == "translate"]
    assert labelled and labelled[0] == "çeviri"

    progressed = [s for s in seen if s["total"] > 0]
    assert progressed, "the translate stage never reported a unit total"
    assert progressed[-1]["completed"] == progressed[-1]["total"] == 10
    final = seen[-1]
    assert final["phase"] == "done", final
    assert final["result"]["chars_sent_this_run"] > 0


# --------------------------------------------------------------------------- #
# The dry-run gate: nothing is sent before the user confirms
# --------------------------------------------------------------------------- #


def test_estimate_shows_the_characters_without_calling_the_provider(
    tmp_path: Path, ten_unit_pdf: Path
) -> None:
    translator = FakeTranslator()
    client = make_client(tmp_path, translator=translator)
    job = upload(client, ten_unit_pdf, mode="overlay", estimate=True).json()

    assert job["phase"] == "awaiting_confirm"
    assert job["estimate"]["units"] == 10
    assert job["estimate"]["chars"] > 0
    assert job["result"] is None
    # the money assertion: the provider was neither asked to translate nor bound
    assert translator.calls == []
    assert translator.prepared == []


def test_confirmation_runs_the_translation_and_counts_what_was_sent(
    tmp_path: Path, ten_unit_pdf: Path
) -> None:
    translator = FakeTranslator()
    client = make_client(tmp_path, translator=translator)
    job = upload(client, ten_unit_pdf, mode="overlay", estimate=True).json()
    estimated = job["estimate"]["chars"]

    started = client.post(f"/api/jobs/{job['id']}/start")
    assert started.status_code == 200
    finished = started.json()
    assert finished["phase"] == "done", finished
    assert translator.calls, "confirming the estimate must run the translation"
    assert finished["result"]["chars_sent_this_run"] == estimated


def test_start_is_refused_while_the_job_runs(tmp_path: Path, overlay_pdfs: Dict[str, Path]) -> None:
    client = make_client(tmp_path)
    job = upload(client, overlay_pdfs["overlay_styles"]).json()
    manager = client.app.state.manager  # type: ignore[attr-defined]
    manager.get(job["id"]).phase = jobs_module.PHASE_RUNNING
    assert client.post(f"/api/jobs/{job['id']}/start").status_code == 409
    assert client.post("/api/jobs/nope/start").status_code == 404


# --------------------------------------------------------------------------- #
# Upload validation
# --------------------------------------------------------------------------- #


def test_a_file_that_is_not_a_pdf_is_refused(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/jobs",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"mode": "reflow", "estimate": "false"},
    )
    assert response.status_code == 400
    assert "PDF" in response.json()["error"]["message"]
    assert list((tmp_path / "web-jobs").glob("*")) == []  # no job directory left behind


def test_a_pdf_extension_with_other_content_is_refused(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/jobs",
        files={"file": ("book.pdf", b"MZ not a pdf at all", "application/pdf")},
        data={"mode": "reflow", "estimate": "false"},
    )
    assert response.status_code == 400
    assert "PDF" in response.json()["error"]["message"]


def test_an_empty_upload_is_refused(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/jobs",
        files={"file": ("book.pdf", b"", "application/pdf")},
        data={"mode": "reflow", "estimate": "false"},
    )
    assert response.status_code == 400


def test_an_unknown_mode_is_refused(tmp_path: Path, overlay_pdfs: Dict[str, Path]) -> None:
    client = make_client(tmp_path)
    assert upload(client, overlay_pdfs["overlay_styles"], mode="pdf").status_code == 400


def test_the_size_limit_is_enforced_while_reading(
    tmp_path: Path, overlay_pdfs: Dict[str, Path]
) -> None:
    client = make_client(tmp_path, max_upload_bytes=1024)
    response = upload(client, overlay_pdfs["overlay_styles"])
    assert response.status_code == 413
    assert "MB" in response.json()["error"]["message"]
    # the refused job leaves nothing behind
    assert list((tmp_path / "web-jobs").glob("*/*")) == []


def test_a_traversal_file_name_cannot_leave_the_job_directory(
    tmp_path: Path, overlay_pdfs: Dict[str, Path]
) -> None:
    client = make_client(tmp_path)
    response = upload(client, overlay_pdfs["overlay_styles"], name="../../../evil.pdf")
    assert response.status_code == 201
    job = response.json()
    assert job["filename"] == "evil.pdf"
    job_dir = tmp_path / "web-jobs" / job["id"]
    assert (job_dir / "input.pdf").is_file()
    assert not (tmp_path / "evil.pdf").exists()
    assert not (tmp_path.parent / "evil.pdf").exists()
    assert list(tmp_path.glob("*.pdf")) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("..\\..\\windows\\evil.pdf", "evil.pdf"),
        ("", "input.pdf"),
        ("..", "input.pdf"),
        ("a\r\nb.pdf", "a__b.pdf"),
        ("C:/tmp/book.pdf", "book.pdf"),
    ],
)
def test_safe_filename_strips_every_path_element(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected


# --------------------------------------------------------------------------- #
# Glossaries
# --------------------------------------------------------------------------- #


def test_glossaries_are_listed_and_only_a_listed_one_is_accepted(
    tmp_path: Path, overlay_pdfs: Dict[str, Path]
) -> None:
    directory = tmp_path / "glossaries"
    write_glossary(directory, "terms.en-tr.json", "investigation", "inceleme")
    (directory / "notes.md").write_text("not a glossary", encoding="utf-8")
    client = make_client(tmp_path, glossary_dir=directory)

    config = client.get("/api/config").json()
    assert config["glossaries"] == ["terms.en-tr.json"]
    assert config["modes"] == ["overlay", "reflow"]

    accepted = upload(client, overlay_pdfs["overlay_styles"], glossary="terms.en-tr.json")
    assert accepted.status_code == 201
    assert accepted.json()["glossary"] == "terms.en-tr.json"

    for bad in ("missing.json", "../../secret.json", "notes.md"):
        refused = upload(client, overlay_pdfs["overlay_styles"], glossary=bad)
        assert refused.status_code == 400, bad
        assert "Sözlük" in refused.json()["error"]["message"]


def test_no_glossary_directory_is_not_an_error(tmp_path: Path) -> None:
    client = make_client(tmp_path, glossary_dir=tmp_path / "nowhere")
    assert client.get("/api/config").json()["glossaries"] == []


# --------------------------------------------------------------------------- #
# Failures reach the user in plain Turkish
# --------------------------------------------------------------------------- #


def test_a_pdf_without_a_text_layer_is_explained(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    job = upload(client, blank_pdf(tmp_path / "scan.pdf")).json()
    assert job["phase"] == "error"
    error = job["error"]
    assert error["code"] == ErrorCode.NO_TEXT_LAYER.value
    assert error["exit_code"] == 1
    assert "OCR" in error["hint"]
    assert error["message"]


def test_a_permission_restricted_pdf_in_overlay_mode_is_explained(
    tmp_path: Path, overlay_pdfs: Dict[str, Path]
) -> None:
    client = make_client(tmp_path)
    job = upload(client, overlay_pdfs["overlay_encrypted"], mode="overlay").json()
    assert job["phase"] == "error"
    error = job["error"]
    assert error["code"] == ErrorCode.OVERLAY_PDF_RESTRICTED.value
    assert "reflow" in error["hint"]


def test_a_quota_failure_is_explained_and_keeps_the_state(
    tmp_path: Path, ten_unit_pdf: Path
) -> None:
    translator = FakeTranslator(
        prepare_error=AppError(
            ErrorCode.PROVIDER_QUOTA, "quota exceeded (HTTP 456)", ErrorScope.JOB_FATAL
        )
    )
    client = make_client(tmp_path, translator=translator)
    job = upload(client, ten_unit_pdf, mode="overlay").json()
    assert job["phase"] == "error"
    assert job["error"]["code"] == ErrorCode.PROVIDER_QUOTA.value
    assert job["error"]["exit_code"] == 3  # EXIT_PAUSED
    assert "kota" in job["error"]["hint"].lower()


def test_an_unreadable_upload_reports_a_message_not_a_traceback(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    truncated = tmp_path / "broken.pdf"
    truncated.write_bytes(b"%PDF-1.7\nnot really a document")
    job = upload(client, truncated).json()
    assert job["phase"] == "error"
    assert job["error"]["message"]
    assert "Traceback" not in job["error"]["message"]
    assert job["error"]["hint"]


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


def test_the_api_key_never_reaches_the_browser(tmp_path: Path, ten_unit_pdf: Path) -> None:
    """A provider error whose message quotes the key is redacted before it is stored."""
    translator = FakeTranslator(
        prepare_error=AppError(
            ErrorCode.PROVIDER_AUTH,
            f"forbidden (HTTP 403) for key {API_KEY}",
            ErrorScope.JOB_FATAL,
        )
    )
    client = make_client(tmp_path, translator=translator)
    created = upload(client, ten_unit_pdf, mode="overlay")
    job = created.json()
    assert job["error"]["code"] == ErrorCode.PROVIDER_AUTH.value
    assert API_KEY not in created.text
    assert API_KEY not in client.get(f"/api/jobs/{job['id']}").text
    assert API_KEY not in client.get("/api/config").text
    assert API_KEY not in client.get("/").text
    assert "***" in job["error"]["message"]


def test_a_crash_inside_the_job_is_reported_without_internals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overlay_pdfs: Dict[str, Path]
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(f"secret in the traceback: {API_KEY}")

    monkeypatch.setattr(jobs_module, "_TrackingOrchestrator", boom)
    client = make_client(tmp_path)
    created = upload(client, overlay_pdfs["overlay_styles"])
    job = created.json()
    assert job["phase"] == "error"
    assert job["error"]["code"] == ErrorCode.INTERNAL.value
    assert API_KEY not in created.text
    assert "RuntimeError" in job["error"]["message"]
