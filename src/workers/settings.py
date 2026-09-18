"""Shared worker settings."""

from __future__ import annotations

import os
from dataclasses import dataclass

from src.environment import load_project_environment


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    download_concurrency: int
    download_max_attempts: int
    ocr_concurrency: int
    ocr_max_attempts: int
    retry_delays_seconds: tuple[float, ...]

    @classmethod
    def from_environment(cls) -> WorkerSettings:
        load_project_environment()
        values = cls(
            download_concurrency=int(os.getenv("DOWNLOAD_CONCURRENCY", "20")),
            download_max_attempts=int(os.getenv("DOWNLOAD_MAX_ATTEMPTS", "3")),
            ocr_concurrency=int(os.getenv("OCR_CONCURRENCY", "1")),
            ocr_max_attempts=int(os.getenv("OCR_MAX_ATTEMPTS", "3")),
            retry_delays_seconds=tuple(
                float(value.strip())
                for value in os.getenv("QUEUE_RETRY_DELAYS_SECONDS", "1,5").split(",")
                if value.strip()
            ),
        )
        if min(
            values.download_concurrency,
            values.download_max_attempts,
            values.ocr_concurrency,
            values.ocr_max_attempts,
        ) <= 0:
            raise ValueError("Worker concurrency and attempt limits must be positive")
        if any(delay < 0 for delay in values.retry_delays_seconds):
            raise ValueError("Queue retry delays cannot be negative")
        return values

    def retry_delay(self, completed_attempt: int) -> float:
        if not self.retry_delays_seconds:
            return 0
        index = min(completed_attempt - 1, len(self.retry_delays_seconds) - 1)
        return self.retry_delays_seconds[index]
