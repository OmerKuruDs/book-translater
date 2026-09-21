"""Local web UI: upload a PDF, translate it, download the result.

Optional extra (``pip install -e ".[web]"``); the CLI does not depend on it.

    python -m book_translator.web                     # http://127.0.0.1:8765
    uvicorn book_translator.web.app:app --host 127.0.0.1 --port 8765

Loopback only by default and without authentication - it is a single-user local tool.
FastAPI is imported by :mod:`book_translator.web.app`, not here, so importing this
package costs nothing.
"""

from __future__ import annotations
