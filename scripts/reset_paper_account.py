#!/usr/bin/env python3
"""Clear paper trading history and reset the $250 IST paper account."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import engine
from app.models import Base
from app.paper_reset import reset_paper_account


def reset() -> None:
    Base.metadata.create_all(bind=engine)
    result = reset_paper_account()
    print("Paper account reset to", result["initial_balance"], "IST date", result["ist_date"])
    print("Cleared:", result["deleted"])


if __name__ == "__main__":
    reset()
