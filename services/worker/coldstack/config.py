"""Settings. Everything optional at boot: keys are per-workspace, not per-process."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = ""
    redis_url: str = ""
    credential_master_key: str = ""
    reacher_url: str = ""

    @property
    def has_db(self) -> bool:
        return bool(self.database_url)


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", ""),
        redis_url=os.getenv("REDIS_URL", ""),
        credential_master_key=os.getenv("CREDENTIAL_MASTER_KEY", ""),
        reacher_url=os.getenv("REACHER_URL", ""),
    )
