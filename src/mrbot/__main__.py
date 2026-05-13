"""Entry point: `python -m mrbot`."""

from __future__ import annotations

import logging
import os
import sys

from .config import load_config
from .poller import run_forever


def _setup_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # httpx is loud at DEBUG; keep it at WARNING regardless.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> int:
    _setup_logging()
    log = logging.getLogger("mrbot")
    try:
        cfg = load_config()
    except Exception as e:
        log.error("config error: %s", e)
        return 2

    try:
        run_forever(cfg)
    except KeyboardInterrupt:
        log.info("interrupted, exiting")
        return 0
    except Exception:
        log.exception("fatal error in main loop")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
