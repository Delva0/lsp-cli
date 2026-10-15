"""Versioned addresses: symbol/path globs, brace sets and explicit rg expressions."""

import fnmatch
import re
import subprocess
from dataclasses import dataclass, replace
from functools import lru_cache
from itertools import pairwise


@dataclass(frozen=True)
class Selector:
    input: str
    revision: str | None
    pattern: str
    destination: str | None = None
    line: int | None = None
    column: int | None = None
    mode: str = "glob"
    destination_mode: str = "glob"


def braces(value):
    """Expand comma sets, including nested sets; leave non-set braces literal.

    In particular Git's HEAD^{commit} and HEAD^{} are not brace sets. Regex
    bodies never pass through this function, so quantifiers remain intact.
    """
    stack = []
    bracket = False
    for i, char in enumerate(value):
        if char == "[":
            bracket = True
        elif char == "]":
            bracket = False
        elif not bracket and char == "{":
            stack.append((i, []))
        elif not bracket and char == "," and stack:
            stack[-1][1].append(i)
        elif not bracket and char == "}" and stack:
            start, commas = stack.pop()
            if commas:
                bounds = [start, *commas, i]
                alternatives = [value[a + 1 : b] for a, b in pairwise(bounds)]
                if any(not part for part in alternatives):
                    raise ValueError("Brace sets require nonempty alternatives: {A,B}")
                return list(
                    dict.fromkeys(
                        expanded for part in alternatives for expanded in braces(value[:start] + part + value[i + 1 :])
                    ),
                )
    if stack and any(commas for _, commas in stack):
        raise ValueError("Unclosed brace set; use {A,B} and quote the whole target")
    return [value]


def revisions(value):
    if not value:
        return [None]
    values = braces(value)
    if any(not v or v != v.strip() for v in values):
        raise ValueError("A revision set needs nonempty comma-separated revisions")
    return values


def prefix(value, default=None):
    # A colon in a regex group, e.g. (?:foo|bar), is not a revision separator.
    match = re.match(r"^([^:\s()\[\]\\$|*?]+):", value)
    if match:
        revision = match[1]
        if default and revision != default:
            raise ValueError("--rev conflicts with a target revision")
        return revision, value[match.end() :]
    return default, value


def position(value, command):
    match = re.fullmatch(r"(.*):([0-9]+)(?::([0-9]+))?", value)
    # Read the numeric suffix from the right, independently of the revision.
    match = re.fullmatch(r"(.*):([0-9]+):([0-9]+)", value) or match
    if not match or (command == "complete" and match[3] is None):
        tail = "LINE:COLUMN" if command == "complete" else "LINE[:COLUMN]"
        raise ValueError(f"{command} expects [REV:]FILE_GLOB:{tail}")
    line, column = int(match[2]), int(match[3]) if match[3] is not None else None
    if line < 1:
        raise ValueError("Source lines are one-based (LINE >= 1)")
    return match[1], line, column


def inputs(args):
    values = getattr(args, "target_inputs", None)
    if values is None:
        values = [(s, "glob") for s in args.symbols] + [(s, "regex") for s in args.regexp]
    return [(s, "fixed" if args.fixed_strings else mode) for s, mode in values]


def symbol_patterns(pattern, mode):
    """Validate sets before an impossible symbol pattern can scan a repository."""
    patterns = braces(pattern) if mode == "glob" else [pattern]
    if mode == "glob" and "/" not in pattern and not pattern.endswith((".py", ".pyi")):
        for selected in patterns:
            # File names may contain literal braces. Python symbol components
            # cannot; remaining braces are not comma sets (nor regex syntax).
            outside_classes = re.sub(r"\[[^\]]*\]", "", selected)
            if "{" in outside_classes or "}" in outside_classes:
                raise ValueError(
                    f"Invalid symbol set {pattern!r}: use a name without braces for one symbol, "
                    "or {A,B} for multiple symbols; use -e for a regular expression",
                )
    return patterns


def expand(args, values=None):
    values = inputs(args) if values is None else values
    if args.command == "path":
        if len(values) != 2:
            raise ValueError("path expects [REVSET:]FROM TO; write revisions only once")
        (source, mode), (destination, destination_mode) = values
        other_rev, _ = prefix(destination)
        if other_rev is not None:
            raise ValueError("path uses one commit per pair: write revisions only on FROM, not TO")
        revision, pattern = prefix(source, args.rev)
        if not pattern or not destination:
            raise ValueError("path requires two nonempty targets")
        symbol_patterns(pattern, mode)
        symbol_patterns(destination, destination_mode)
        # Keep each endpoint's set intact: matching returns a union and the path
        # engine traverses once per start, not once per brace-expanded pair.
        spec = Selector(source, revision, pattern, destination, mode=mode, destination_mode=destination_mode)
        return [replace(spec, revision=r) for r in revisions(revision or ("HEAD" if args.repo else None))]
    if not values:
        if args.command != "list":
            raise ValueError(f"{args.command} requires at least one target")
        values = [("", "glob")]
    result = []
    for value, mode in values:
        address, line, column = value, None, None
        if args.command in {"where", "complete"}:
            address, line, column = position(value, args.command)
        revision, pattern = prefix(address, args.rev)
        if args.command != "list" and not pattern:
            raise ValueError("A target must not be empty; use ** to match all")
        revision = revision or ("HEAD" if args.repo or args.command == "history" else None)
        spec = Selector(value, revision, pattern, line=line, column=column, mode=mode)
        for rev in revisions(revision):
            if args.command == "diff":
                if revision != rev or not rev or ".." not in rev:
                    raise ValueError("diff requires exactly two revisions: OLD..NEW:TARGET")
                old, new = rev.split("..", 1)
                if not old or not new or new.startswith(".") or ".." in new:
                    raise ValueError("diff requires exactly two revisions: OLD..NEW:TARGET")
            patterns = ((braces(pattern) if mode == "glob" else [pattern])
                        if args.command in {"where", "complete"} else symbol_patterns(pattern, mode))
            result.extend(
                replace(spec, revision=rev, pattern=selected)
                for selected in patterns
            )
    return result


@lru_cache(maxsize=2048)
def glob_regex(pattern, separator, ignore_case=False, suffix=False):
    """Translate shell-style globs with an explicit hierarchy separator."""
    out, i = [], 0
    sep = re.escape(separator)
    while i < len(pattern):
        char = pattern[i]
        i += 1
        if char == "*":
            if i < len(pattern) and pattern[i] == "*":
                while i < len(pattern) and pattern[i] == "*":
                    i += 1
                if i < len(pattern) and pattern[i] == separator:
                    out.append(f"(?:.*{sep})?")
                    i += 1
                else:
                    out.append(".*")
            else:
                out.append(f"[^{sep}]*")
        elif char == "?":
            out.append(f"[^{sep}]")
        elif char == "[":
            end = i + (i < len(pattern) and pattern[i] == "!")
            end += end < len(pattern) and pattern[end] == "]"
            end = pattern.find("]", end)
            if end < 0:
                out.append(r"\[")
            else:
                translated = fnmatch.translate(pattern[i - 1 : end + 1])
                inner = translated.removeprefix("(?s:").removesuffix(")\\Z").removesuffix(")\\z")
                out.append(f"(?=[^{sep}])" + inner)
                i = end + 1
        else:
            out.append(re.escape(char))
    start = f"(?:^|{sep})" if suffix else r"\A"
    return re.compile(start + "".join(out) + r"\Z", re.DOTALL | (re.IGNORECASE if ignore_case else 0))


class Matcher:
    def __init__(self, args):
        self.ignore_case = args.ignore_case
        self.options = [flag for enabled, flag in ((args.pcre2, "-P"), (args.ignore_case, "-i")) if enabled]
        self.cache = {}

    def indices(self, pattern, names, *, mode="glob", domain="symbol"):
        """Original candidate indices; glob is whole-path or symbol suffix matching."""
        names = tuple(names)
        key = pattern, names, mode, domain
        if key not in self.cache:
            if mode == "glob":
                separator = "/" if domain == "file" else "."
                patterns = braces(pattern)
                if domain == "file":
                    patterns = [p.removeprefix("./") for p in patterns]
                    patterns = ["**" if p in {"", "."} else p for p in patterns]
                    canonical = str.casefold if self.ignore_case else str
                    # Directory targets and explicit trailing slashes share the
                    # same recursive meaning in --files and position targets.
                    patterns = [
                        p.rstrip("/") + "/**"
                        if p.endswith("/") or any(
                            canonical(n).startswith(canonical(p.rstrip("/")) + "/") for n in names
                        )
                        else p
                        for p in patterns
                    ]
                compiled = [glob_regex(p, separator, self.ignore_case, domain == "symbol") for p in patterns]
                self.cache[key] = tuple(i for i, name in enumerate(names) if any(rx.search(name) for rx in compiled))
            else:
                self.cache[key] = self.regex_indices(pattern, names, fixed=mode == "fixed")
        return self.cache[key]

    def regex_indices(self, pattern, names, *, fixed=False):
        if any("\n" in name or "\r" in name for name in names):
            raise ValueError("Names containing newlines cannot be matched by rg")
        options = ["-F", *[o for o in self.options if o != "-P"]] if fixed else self.options
        try:
            found = subprocess.run(
                [
                    "rg",
                    "--no-config",
                    "--color",
                    "never",
                    "--text",
                    "-n",
                    "--no-heading",
                    *options,
                    "-e",
                    pattern,
                    "--",
                    "-",
                ],
                input="".join(name + "\n" for name in names),
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as error:
            raise ValueError("ripgrep (rg) is required for -e/-F matching; -P requires PCRE2 support") from error
        if found.returncode not in (0, 1):
            raise ValueError("rg: " + found.stderr.strip())
        return tuple(int(line.partition(":")[0]) - 1 for line in found.stdout.splitlines())

    def entries(self, pattern, entries, *, mode="glob"):
        if mode == "glob":
            return [entries[i] for i in self.indices(pattern, [e.name for e in entries], mode=mode)]
        names = [name for entry in entries for name in (entry.name, entry.name.rsplit(".", 1)[-1])]
        selected = {i // 2 for i in self.indices(pattern, names, mode=mode)}
        return [entry for i, entry in enumerate(entries) if i in selected]


def literal_address(pattern, mode="glob"):
    """A concrete address can also resolve a proven inherited/aliased binding."""
    if mode != "glob":
        return None
    return pattern if all(part.isidentifier() for part in pattern.split(".")) else None
