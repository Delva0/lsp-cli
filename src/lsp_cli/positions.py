"""An invocation-local interval tree for repeated exact source position lookups."""


class Positions:
    def __init__(self, source):
        spans = []
        for order, node in enumerate(source.syntax):
            if not hasattr(node, "lineno"):
                continue
            span = source.span(node)
            spans.append(((span["line"], span["column"]), (span["end_line"], span["end_column"]), order, node))
        spans.sort(key=lambda row: (row[0], row[2]))

        def build(lo, hi):
            if lo >= hi:
                return None
            mid = (lo + hi) // 2
            row = spans[mid]
            left, right = build(lo, mid), build(mid + 1, hi)
            end = max(row[1], left[0] if left else row[1], right[0] if right else row[1])
            return end, row, left, right

        self.tree = build(0, len(spans))

    def at(self, point):
        found, pending = [], [self.tree]
        while pending:
            branch = pending.pop()
            if branch is None or branch[0] <= point:
                continue
            _end, (start, end, order, node), left, right = branch
            pending.append(left)
            if start <= point:
                if point < end:
                    found.append((order, node))
                pending.append(right)
        # Preserve ast.walk's tie order (e.g. Call and Attribute sharing a span).
        return [node for _order, node in sorted(found, key=lambda item: item[0])]
