"""Layering contract of design doc 02, section 1.4, checked on the AST of every module in src/.

Allowed import direction only:

* ``cli`` / ``web`` -> config, logging_setup, pipeline, domain (+ the service registries
  ``*.base``); ``web`` is the optional FastAPI front end and sits beside the CLI
* ``pipeline``     -> extractors, glossary, translators, exporters, domain, config, logging_setup,
  ``database.repository`` and ``database.session`` (never ``database.models`` / raw SQL)
* services (extractors / glossary / translators / exporters) -> domain, config
* ``database``     -> domain
* ``domain``       -> stdlib and pydantic only
* ``config``       -> domain; ``logging_setup`` -> nothing internal
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import book_translator

PACKAGE = "book_translator"
SRC = Path(book_translator.__file__).resolve().parent
SERVICES: Set[str] = {"extractors", "glossary", "translators", "exporters"}
REGISTRIES: Set[str] = {f"{PACKAGE}.{service}.base" for service in SERVICES}
PIPELINE_DATABASE: Set[str] = {f"{PACKAGE}.database.repository", f"{PACKAGE}.database.session"}
FRONT_ENDS: Set[str] = {"cli", "web"}  # may reach the service registries, nothing else
DOMAIN_EXTERNAL: Set[str] = {"pydantic", "__future__"}

ALLOWED_LAYERS: Dict[str, Set[str]] = {
    "root": set(),
    "cli": {"config", "logging_setup", "pipeline", "domain"},
    "web": {"config", "logging_setup", "pipeline", "domain"},
    "config": {"domain"},
    "logging_setup": set(),
    "pipeline": SERVICES | {"domain", "config", "logging_setup"},
    "extractors": {"domain", "config", "pdfkit"},
    "glossary": {"domain", "config"},
    "translators": {"domain", "config"},
    "exporters": {"domain", "config", "pdfkit"},
    "pdfkit": set(),  # leaf: stdlib + pymupdf only (design doc 06, D17)
    "database": {"domain"},
    "domain": set(),
}


def module_name(path: Path) -> str:
    parts = [PACKAGE, *path.relative_to(SRC).with_suffix("").parts]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def layer_of(module: str) -> str:
    parts = module.split(".")
    if len(parts) < 2 or parts[1].startswith("__"):
        return "root"  # the package itself (``__version__``)
    return parts[1]


def imports_of(path: Path) -> List[Tuple[str, bool]]:
    """``(target, absolute)`` per imported name; relative targets are resolved to full names."""
    module = module_name(path)
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found: List[Tuple[str, bool]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend((alias.name, True) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = parts[: len(parts) - (node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
                found.extend((f"{target}.{alias.name}", False) for alias in node.names)
            else:
                target = node.module or ""
                found.extend((f"{target}.{alias.name}", True) for alias in node.names)
    return found


def violation(module: str, target: str) -> Optional[str]:
    """Description of the broken rule for ``module -> target``, or ``None`` when allowed."""
    if not target.startswith(PACKAGE + "."):
        return None
    source_layer = layer_of(module)
    target_layer = layer_of(target)
    if target_layer == "root":
        return None  # the package root only holds ``__version__``
    if source_layer == target_layer and source_layer != "root":
        return None
    if source_layer in FRONT_ENDS and target_layer in SERVICES:
        if any(target == r or target.startswith(r + ".") for r in REGISTRIES):
            return None
        return f"{module} -> {target} ({source_layer} may only use the service registries)"
    if source_layer == "pipeline" and target_layer == "database":
        if any(target == m or target.startswith(m + ".") for m in PIPELINE_DATABASE):
            return None
        return f"{module} -> {target} (pipeline may only use database.repository/session)"
    if target_layer in ALLOWED_LAYERS.get(source_layer, set()):
        return None
    return f"{module} -> {target}"


def test_import_direction_follows_design_1_4() -> None:
    modules = sorted(SRC.rglob("*.py"))
    assert len(modules) >= 46, modules
    violations = [
        found
        for path in modules
        for target, _ in imports_of(path)
        if (found := violation(module_name(path), target)) is not None
    ]
    assert violations == []


def test_domain_uses_only_stdlib_and_pydantic() -> None:
    offenders: List[str] = []
    for path in sorted((SRC / "domain").glob("*.py")):
        for target, absolute in imports_of(path):
            if not absolute:
                continue  # relative import inside the domain package
            top = target.split(".")[0]
            if top in DOMAIN_EXTERNAL or top in sys.stdlib_module_names:
                continue
            if top == PACKAGE and layer_of(target) == "domain":
                continue
            offenders.append(f"{path.name}: {target}")
    assert offenders == []


def test_checker_recognises_the_forbidden_edges() -> None:
    """The contract test is not vacuous: each forbidden direction of 1.4 is reported."""
    assert violation(f"{PACKAGE}.pipeline.orchestrator", f"{PACKAGE}.database.models.Job")
    assert violation(f"{PACKAGE}.cli", f"{PACKAGE}.database.repository.JobRepository")
    assert violation(f"{PACKAGE}.cli", f"{PACKAGE}.translators.deepl_translator.DeepLTranslator")
    assert violation(f"{PACKAGE}.exporters.base", f"{PACKAGE}.glossary.manager.effective_glossary")
    assert violation(f"{PACKAGE}.translators.base", f"{PACKAGE}.pipeline.segmenter.segment")
    assert violation(f"{PACKAGE}.database.repository", f"{PACKAGE}.glossary.schema.Glossary")
    assert violation(f"{PACKAGE}.domain.models", f"{PACKAGE}.config.Settings")
    assert violation(f"{PACKAGE}.pipeline.orchestrator", f"{PACKAGE}.database.repository.X") is None
    # module-level forms: ``from ..database import repository`` / ``session`` are allowed,
    # ``from ..database import models`` and ``from .. import database`` are not (CR-36)
    assert violation(f"{PACKAGE}.pipeline.orchestrator", f"{PACKAGE}.database.repository") is None
    assert violation(f"{PACKAGE}.pipeline.orchestrator", f"{PACKAGE}.database.session") is None
    assert violation(f"{PACKAGE}.pipeline.orchestrator", f"{PACKAGE}.database.models")
    assert violation(f"{PACKAGE}.pipeline.orchestrator", f"{PACKAGE}.database")
    assert violation(f"{PACKAGE}.pipeline.orchestrator", f"{PACKAGE}.database.sessions.x")
    assert violation(f"{PACKAGE}.cli", f"{PACKAGE}.translators.base.get_translator") is None
    assert violation(f"{PACKAGE}.web.jobs", f"{PACKAGE}.exporters.base.ExportOptions") is None
    assert violation(f"{PACKAGE}.web.jobs", f"{PACKAGE}.pipeline.orchestrator.X") is None
    assert violation(f"{PACKAGE}.web.jobs", f"{PACKAGE}.database.repository.JobRepository")
    assert violation(f"{PACKAGE}.web.jobs", f"{PACKAGE}.translators.deepl_translator.X")
    assert violation(f"{PACKAGE}.exporters.epub_exporter", f"{PACKAGE}.exporters.base.x") is None
    assert violation(f"{PACKAGE}.cli", f"{PACKAGE}.__version__") is None
    assert violation(f"{PACKAGE}.exporters.base", "markdown.markdown") is None
    # v1.1 pdfkit leaf layer (design doc 06, §2.5)
    pdf_api = f"{PACKAGE}.pdfkit.api"
    assert violation(f"{PACKAGE}.extractors.pymupdf_extractor", f"{pdf_api}.open_document") is None
    assert violation(f"{PACKAGE}.exporters.pdf_native", f"{pdf_api}.new_document") is None
    assert violation(f"{PACKAGE}.pipeline.orchestrator", f"{PACKAGE}.pdfkit.api.open_document")
    assert violation(f"{PACKAGE}.pdfkit.api", f"{PACKAGE}.domain.models.Chunk")
    assert violation(f"{PACKAGE}.exporters.pdf_overlay_exporter", f"{PACKAGE}.extractors.cleanup.x")
    assert violation(f"{PACKAGE}.database.repository", f"{PACKAGE}.extractors.figure_inventory.x")
