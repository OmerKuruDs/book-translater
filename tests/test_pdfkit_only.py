"""Only ``pdfkit/`` talks to PyMuPDF (design doc 06, D17; v1.1 review CR-68).

``tests/test_layering.py`` checks the direction of *internal* imports; this test pins the
external one: no module of ``src/`` outside ``pdfkit/`` imports ``pymupdf`` or its legacy
alias ``fitz`` - neither at module level nor inside a function, neither with ``import``
nor through ``importlib`` / ``__import__`` with a literal name.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Set

import book_translator

SRC = Path(book_translator.__file__).resolve().parent
PDF_LIBRARIES: Set[str] = {"pymupdf", "fitz"}
DYNAMIC_IMPORTERS: Set[str] = {"import_module", "__import__", "find_spec"}


def _root(name: str) -> str:
    return name.split(".", 1)[0]


def pdf_library_imports(tree: ast.AST) -> List[str]:
    """``line: statement`` for every import of a PDF library in ``tree``."""
    found: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names if _root(alias.name) in PDF_LIBRARIES]
            found.extend(f"{node.lineno}: import {name}" for name in names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and _root(node.module) in PDF_LIBRARIES:
                found.append(f"{node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in DYNAMIC_IMPORTERS and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    if _root(first.value) in PDF_LIBRARIES:
                        found.append(f"{node.lineno}: {name}({first.value!r})")
    return found


def test_only_pdfkit_imports_pymupdf() -> None:
    offenders: List[str] = []
    importers: List[str] = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        hits = pdf_library_imports(ast.parse(path.read_text(encoding="utf-8"), filename=relative))
        if not hits:
            continue
        if relative.startswith("pdfkit/"):
            importers.append(relative)
        else:
            offenders.extend(f"{relative}:{hit}" for hit in hits)
    assert offenders == [], "pymupdf must only be imported by pdfkit/: " + "; ".join(offenders)
    assert importers == ["pdfkit/api.py"]  # the wrapper exists and is the single entry point


def test_the_detector_sees_every_import_form() -> None:
    source = (
        "import os\n"
        "import pymupdf\n"
        "import fitz as legacy\n"
        "from pymupdf import Rect\n"
        "from pymupdf.utils import getColor\n"
        "from . import pymupdf_extractor\n"
        "def lazy():\n"
        "    import pymupdf.table\n"
        "    importlib.import_module('fitz')\n"
        "    __import__('pymupdf')\n"
        "    importlib.import_module('weasyprint')\n"
    )
    hits = pdf_library_imports(ast.parse(source))
    assert [hit.split(": ", 1)[1] for hit in sorted(hits, key=lambda h: int(h.split(":")[0]))] == [
        "import pymupdf",
        "import fitz",
        "from pymupdf import ...",
        "from pymupdf.utils import ...",
        "import pymupdf.table",
        "import_module('fitz')",
        "__import__('pymupdf')",
    ]
