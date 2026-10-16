"""Minimal, dependency-free ``.env`` loader.

Local secrets (Cloudinary credentials, the optional front-end URL) live in a
gitignored ``.env`` file at the project root. **Real environment variables
always win**, so exported values and CI settings are never overwritten.

File format::

    CLOUDINARY_CLOUD_NAME=my-cloud
    CLOUDINARY_API_KEY=1234567890
    CLOUDINARY_API_SECRET=abcdef

Blank lines and lines starting with ``#`` are ignored; surrounding single or
double quotes are stripped from values.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

__all__ = ["load_dotenv", "project_root"]


def project_root() -> Path:
    """The repository root (parent of the ``plantreadiness`` package)."""
    return Path(__file__).resolve().parent.parent


def load_dotenv(path: Optional[Union[str, Path]] = None,
                override: bool = False) -> Optional[Path]:
    """Load ``KEY=VALUE`` pairs from a ``.env`` file into ``os.environ``.

    Returns the path that was read, or ``None`` when no file exists. Existing
    environment variables are preserved unless ``override`` is ``True``.
    """
    env_path = Path(path) if path is not None else project_root() / ".env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
    return env_path
