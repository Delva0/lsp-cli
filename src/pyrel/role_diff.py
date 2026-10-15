"""Diff selected source lines without inventing coordinates across omitted gaps."""

import difflib

from .model import source_lines


def lines(rows):
    result = {}
    for row in rows:
        for number, text in enumerate(source_lines(row["code"] + "\n"), row["line"]):
            result[number] = text.rstrip("\r\n")
    return sorted(result.items())


def selected_diff(before, after, context=3):
    old, new = lines(before), lines(after)
    matcher = difflib.SequenceMatcher(None, [text for _, text in old], [text for _, text in new], autojunk=False)
    full_opcodes = list(matcher.get_opcodes())
    if all(tag == "equal" for tag, *_ in full_opcodes):
        return "", []
    # Retain signature/control headers even when an omitted source interval or
    # the context budget separates them from the changed operation.
    old_context = {n for row in before for n in row.get("context_lines", [])}
    new_context = {n for row in after for n in row.get("context_lines", [])}
    nearby = set()
    for group in matcher.get_grouped_opcodes(context):
        for tag, i, end_i, j, end_j in group:
            if tag == "equal":
                nearby.update(zip(range(i, end_i), range(j, end_j), strict=True))
    groups = []
    for opcodes in [full_opcodes]:
        rows = []
        for tag, i, end_i, j, end_j in opcodes:
            if tag == "equal":
                rows.extend(
                    {"kind": "context", "old_line": old[a][0], "new_line": new[b][0], "text": old[a][1]}
                    for a, b in zip(range(i, end_i), range(j, end_j), strict=True)
                    if (a, b) in nearby or old[a][0] in old_context or new[b][0] in new_context
                )
            else:
                rows.extend(
                    {
                        "kind": "delete",
                        "old_line": old[a][0],
                        "new_line": None,
                        "new_anchor": new[j - 1][0] if j else 0,
                        "text": old[a][1],
                    }
                    for a in range(i, end_i)
                )
                rows.extend(
                    {
                        "kind": "add",
                        "old_line": None,
                        "new_line": new[b][0],
                        "old_anchor": old[end_i - 1][0] if end_i else 0,
                        "text": new[b][1],
                    }
                    for b in range(j, end_j)
                )
        # An omitted physical line is a hunk boundary, even when two lines are
        # adjacent in the selected projection. Every consumed hunk range is real.
        group, cursors = [], {"old_line": None, "new_line": None}
        for row in rows:
            gap = any(
                row[key] is not None and cursors[key] is not None and row[key] != cursors[key] + 1 for key in cursors
            )
            if gap:
                groups.append(group)
                group, cursors = [], {"old_line": None, "new_line": None}
            group.append(row)
            for key in cursors:
                if row[key] is not None:
                    cursors[key] = row[key]
        if group:
            groups.append(group)
    hunks, patch = [], []
    if groups:
        patch = [
            "--- " + (before[0]["source"] if before else "/dev/null"),
            "+++ " + (after[0]["source"] if after else "/dev/null"),
        ]
    for group in groups:
        ranges = {}
        for side, key in (("before", "old_line"), ("after", "new_line")):
            numbers = [r[key] for r in group if r[key] is not None]
            anchor = group[0].get("old_anchor" if side == "before" else "new_anchor", 0)
            ranges[side] = {"line": numbers[0] if numbers else anchor, "count": len(numbers)}
            fragments = before if side == "before" else after
            for row in group:
                number = row[key]
                row[side] = (
                    {"source": fragments[0]["source"], "line": number, "column": 0,
                     "end_line": number, "end_column": len(row["text"])}
                    if number is not None else None
                )
        a, b = ranges["before"], ranges["after"]
        patch.append(f"@@ -{a['line']},{a['count']} +{b['line']},{b['count']} @@")
        patch.extend({"context": " ", "delete": "-", "add": "+"}[r["kind"]] + r["text"] for r in group)
        hunks.append({**ranges, "lines": group})
    return "\n".join(patch), hunks
