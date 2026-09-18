"""Component seven: the thirty-minute cadence.

Uses APScheduler's BlockingScheduler with max_instances=1 so runs never
overlap, and optionally serves the confirmation page from the same process.
A plain cron entry calling `mvsync run` is an equally valid alternative and is
documented in the README.
"""

from __future__ import annotations

import logging
from typing import Callable

from .config import Settings

log = logging.getLogger(__name__)


def run_forever(settings: Settings, job: Callable[[], None], run_immediately: bool = True) -> None:
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
    except ImportError as exc:  # pragma: no cover - environment specific
        raise RuntimeError("apscheduler is not installed: pip install apscheduler") from exc

    scheduler = BlockingScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})
    scheduler.add_job(job, "interval", minutes=settings.sync_interval_minutes, id="sync")
    log.info("scheduled sync every %d minutes", settings.sync_interval_minutes)
    if run_immediately:
        job()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")
