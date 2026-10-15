"""One path-glob contract for explicit sources and position queries."""

import os
from pathlib import Path

from .selectors import Matcher, braces


def local_candidates(pattern, *, ignore_case=False):
    """Walk only the literal directory prefix; do not follow directory symlinks."""
    path = Path(pattern)
    if path.is_file() and not ignore_case:
        return [path.as_posix()]
    if path.is_dir():
        root = path
    else:
        pieces = path.parts
        index = next((i for i, part in enumerate(pieces) if any(c in part for c in "*?[")), len(pieces) - 1)
        root = Path(*pieces[:index]) if index else Path(".")
    roots = [root] if root.is_dir() else []
    if ignore_case:
        roots = [Path(root.anchor or ".")]
        parts = root.parts[1:] if root.anchor else root.parts
        for part in parts:
            if part in {".", ".."}:
                roots = [directory / part for directory in roots]
            else:
                roots = [child for directory in roots for child in directory.iterdir()
                         if child.is_dir() and child.name.casefold() == part.casefold()]

    def fail(error):
        raise error

    return sorted(
        (Path(directory) / name).as_posix()
        for directory_root in roots
        for directory, _dirs, files in os.walk(directory_root, onerror=fail, followlinks=False)
        for name in files
        if (name.casefold() if ignore_case else name).endswith(".py")
    )


def select_files(args, paths=None):
    """Expand --files against the Git tree or disk, never against both."""
    matcher = Matcher(args)
    selected, seen = [], set()
    for specification in args.source:
        module, sep, pattern = specification.partition("=")
        if not sep:
            pattern, module = module, ""
        if paths is not None and (module or pattern == "-"):
            raise ValueError("Git --files uses paths in the selected revision; MODULE=PATH and stdin are local inputs")
        found = []
        for value in braces(pattern):
            if value == "-":
                found.append(value)
                continue
            names = list(paths) if paths is not None else local_candidates(value, ignore_case=args.ignore_case)
            found.extend(names[i] for i in matcher.indices(value, names, domain="file"))
        found = list(dict.fromkeys(found))
        if not found:
            raise ValueError(
                f"No files match {pattern!r} in "
                + (f"revision {args.rev}" if paths is not None else "the working tree"),
            )
        if module and len(found) != 1:
            raise ValueError("MODULE=PATH must select exactly one file; assign each module separately")
        for name in found:
            identity = name if paths is not None or name == "-" else str(Path(name).resolve())
            key = module, identity
            if key not in seen:
                seen.add(key)
                selected.append(module + "=" + name if module else name)
    return selected
