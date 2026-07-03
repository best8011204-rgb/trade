from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class StoredEvent:
    id: str
    type: str
    payload_json: str
    created_at: datetime

