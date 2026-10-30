import logging

import structlog


def configure_logging(*, json_logs: bool = False, level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # The Voyage SDK logs every refused request verbosely; the embedder logs its own short retry line.
    logging.getLogger("voyageai").setLevel(logging.CRITICAL)
    renderer = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            renderer,
        ]
    )
