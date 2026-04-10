"""Entry point for ``python -m artimesone``."""

from __future__ import annotations

import uvicorn

from artimesone.app import create_app
from artimesone.config import Settings


def main() -> None:
    """Boot the ArtimesOne server."""
    settings = Settings()
    uvicorn.run(create_app(), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
