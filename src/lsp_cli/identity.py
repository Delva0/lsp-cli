"""Identify the installed implementation independently of its checkout or wheel path."""

import hashlib
from pathlib import Path

from . import __version__


def source_identity():
    """Return the version and hashes of all installed Python sources, including workers."""
    root = Path(__file__).parent
    files = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(root.rglob("*.py"))}
    return {"version": __version__, "files": files}
