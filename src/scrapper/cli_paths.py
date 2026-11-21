from __future__ import annotations

import os
from pathlib import Path


def existing_file(value: str) -> Path:
    path = Path(value).expanduser().absolute()
    if not path.is_file() or not os.access(path, os.R_OK):
        raise ValueError(f"Expected a readable file: {path}")
    return path
