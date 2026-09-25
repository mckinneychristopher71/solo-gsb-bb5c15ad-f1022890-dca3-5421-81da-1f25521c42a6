"""Application configuration.

Every setting can be overridden through an environment variable so the same
image runs both locally and inside Docker (see ``docker-compose.yml``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    db_path: str
    sample_csv: str
    # A candidate must score at least this high to be accepted automatically.
    auto_threshold: float
    # The best and second-best candidate must differ by at least this much,
    # otherwise the result is ambiguous and waits for human confirmation.
    margin_threshold: float
    # Below this score even the best candidate is considered "no match".
    reject_threshold: float
    # Weight of the street/name token score in the final score
    # (the remainder is the house-number score).
    token_weight: float
    # Seconds a claimed review lease stays valid before the record can be
    # claimed again (default 10 minutes).
    review_lease_seconds: int
    host: str
    port: int

    @property
    def db_url(self) -> str:
        # SQLite needs an absolute path; relative paths are resolved against CWD.
        path = Path(self.db_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path.as_posix()}"


def get_settings() -> Settings:
    return Settings(
        db_path=os.getenv("ADDRESS_DB_PATH", "/data/addresses.db"),
        sample_csv=os.getenv("SAMPLE_CSV", "/app/sample_data/standard_addresses.csv"),
        auto_threshold=_env_float("AUTO_THRESHOLD", 0.80),
        margin_threshold=_env_float("MARGIN_THRESHOLD", 0.06),
        reject_threshold=_env_float("REJECT_THRESHOLD", 0.35),
        token_weight=_env_float("TOKEN_WEIGHT", 0.7),
        review_lease_seconds=_env_int("REVIEW_LEASE_SECONDS", 600),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
    )
