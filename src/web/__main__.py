"""``python -m book_translator.web`` - start the local server.

Binds ``127.0.0.1`` unless ``--host`` says otherwise; there is no authentication, so
any other address exposes the tool (and the PDFs it has already translated) to the
network. That case is called out on startup.
"""

from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path
from typing import Optional, Sequence

import uvicorn

from .app import DEFAULT_HOST, DEFAULT_PORT, create_app


def _is_loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m book_translator.web",
        description="book-translator yerel web arayüzü (PDF yükle -> çevir -> indir).",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="Dinlenecek adres.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Dinlenecek port.")
    parser.add_argument(
        "--work-root",
        type=Path,
        default=None,
        help="İş dizinlerinin kökü (varsayılan: ./web-jobs).",
    )
    parser.add_argument(
        "--glossaries",
        type=Path,
        default=None,
        help="Sözlük dizini (varsayılan: ./glossaries).",
    )
    args = parser.parse_args(argv)

    if not _is_loopback(args.host):
        print(
            f"uyarı: {args.host} adresine bağlanılıyor. Bu araçta kimlik doğrulama yoktur; "
            "yerel ağdaki herkes yüklediğiniz ve çevirdiğiniz PDF'lere erişebilir."
        )
    app = create_app(work_root=args.work_root, glossary_dir=args.glossaries)
    print(f"book-translator: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
