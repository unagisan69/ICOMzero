"""Small, private updater timing log. Never records URLs, credentials or payloads."""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class TimingLog:
    def __init__(self, path=None):
        self.path = Path(path) if path else None

    def record(self, stage, seconds, status="ok"):
        entry = dict(
            time=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            stage=stage,
            seconds=round(seconds, 3),
            status=status,
        )
        log.info("Update stage=%s seconds=%.3f status=%s", stage, seconds, status)
        if not self.path:
            return
        try:
            if self.path.exists() and self.path.stat().st_size > 256 * 1024:
                self.path.replace(self.path.with_name(self.path.name + ".1"))
            fd = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "a") as f:
                os.fchmod(f.fileno(), 0o600)
                f.write(json.dumps(entry) + "\n")
        except OSError:
            log.warning("Could not write update timing log")
