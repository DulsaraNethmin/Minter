"""Entrypoint.

`python main.py` serves the API. `python main.py --init` downloads and verifies the
browser binary then exits — used as a Docker build step so the image ships with the
browser already extracted.
"""

import sys

import uvicorn

from minter.config import HOST, LOG_LEVEL, PORT, configure_logging

logger = configure_logging()


def _init() -> None:
    import invisible_playwright as ip

    path = ip.ensure_binary(status=lambda phase: logger.info("browser: %s", phase))
    logger.info("browser ready at %s", path)


if __name__ == "__main__":
    if "--init" in sys.argv:
        _init()
    else:
        uvicorn.run(
            "minter.app:app",
            host=HOST,
            port=PORT,
            log_level=LOG_LEVEL,
        )
