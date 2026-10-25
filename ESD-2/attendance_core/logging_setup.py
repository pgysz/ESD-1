"""Structured JSON logging (same schema as A1, plus trace_id).

One JSON object per line:
    time, service, severity, message, request_id, trace_id
No user-identifying information is ever logged. request_id and trace_id are
read from context variables, so every log line written while handling a
request carries them automatically -- in both the HTTP and gRPC processes.
"""

import json
import logging
import os
from datetime import datetime, timezone

from . import config, tracing


class JsonFormatter(logging.Formatter):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def format(self, record):
        message = record.getMessage()
        if record.exc_info:
            message += " | " + self.formatException(record.exc_info).replace("\n", " / ")
        return json.dumps({
            "time": datetime.now(timezone.utc).isoformat(),
            "service": self.service,
            "severity": record.levelname,
            "message": message,
            "request_id": tracing.current_request_id(),
            "trace_id": tracing.current_trace_id(),
        })


def configure_logging(service, filename):
    """Create (once) a logger that appends JSON lines to logs/<filename>."""
    logger = logging.getLogger(f"attendance.{service}")
    if getattr(logger, "_configured", False):
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    os.makedirs(config.log_dir(), exist_ok=True)
    handler = logging.FileHandler(os.path.join(config.log_dir(), filename))
    handler.setFormatter(JsonFormatter(service))
    logger.addHandler(handler)
    logger._configured = True
    return logger


def log_event(logger, severity, message):
    logger.log(getattr(logging, severity.upper(), logging.INFO), message)
