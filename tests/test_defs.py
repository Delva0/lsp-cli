"""Definition inventories stay scoped without hiding the full binding tree."""

import pytest

from .test_actions import invoke, query
from .test_git_sources import git, write
from .test_git_sources import repository as repository  # noqa: PLC0414

SOURCE = """LIMIT = 3
class Worker:
    count = 0
    def run(self, value):
        self.state = value
        local = value
        def helper(arg):
            nested = arg
            return nested
        items = [item for item in value]
        convert = lambda param: param
        return helper(local)
class WorkerExtra:
    def other(self): return 1
"""


def names(result):
    return [row["symbol"].removeprefix("__stdin__.") for row in result["results"]]


def test_default_module_and_class_inventory_stops_at_direct_members():
    module = query("list", source=SOURCE)
    assert names(module) == ["__stdin__", "LIMIT", "Worker", "WorkerExtra"]
    result = query("list", "Worker", source=SOURCE)
    assert names(result) == ["Worker", "Worker.count", "Worker.run", "Worker.state"]
    assert result["max_depth"] == 1 and result["truncated"]
    assert result["returned"] == 4 < result["matched"]
    assert result["results"][-1]["parent"] == "__stdin__.Worker"
    assert result["results"][-1]["line"] == 5


def test_function_target_keeps_parameters_locals_and_nested_declarations():
    result = query("list", "Worker.run", source=SOURCE)
    assert names(result) == [
        "Worker.run",
        "Worker.run.self",
        "Worker.run.value",
        "Worker.run.local",
        "Worker.run.helper",
        "Worker.run.items",
        "Worker.run.convert",
    ]
    assert result["truncated"]
    assert all(row["source"] == "<stdin>" for row in result["results"])


def test_explicit_depth_kinds_and_full_tree_are_independent():
    shallow = query("list", "Worker", "--max-depth", "0", source=SOURCE)
    assert names(shallow) == ["Worker"]
    methods = query("list", "Worker", "--kinds", "method", source=SOURCE)
    assert names(methods) == ["Worker.run"] and not methods["truncated"]
    parameters = query("list", "Worker", "--max-depth", "2", "--kinds", "parameter", source=SOURCE)
    assert names(parameters) == ["Worker.run.self", "Worker.run.value"]
    full = query("list", "Worker", "--recursive", source=SOURCE)
    assert full["max_depth"] is None and not full["truncated"]
    assert full["returned"] == full["matched"]
    assert "Worker.run.helper.arg" in names(full)
    assert "Worker.run.helper.nested" in names(full)
    assert any("<listcomp>" in name and name.endswith(".item") for name in names(full))
    assert any("<lambda>" in name and name.endswith(".param") for name in names(full))
    assert not any(name.startswith("WorkerExtra") for name in names(full))
    variables = query("list", "Worker", "--recursive", "--kinds", "parameter", "variable", source=SOURCE)
    assert {row["kind"] for row in variables["results"]} == {"parameter", "variable"}
    assert not variables["truncated"]


def test_depth_omission_does_not_report_false_absence_or_pollute_rows():
    status, output, error = invoke("list", "Worker", "--kinds", "parameter", source=SOURCE)
    # Scope heads are JSON metadata, not result rows bypassing --kinds.
    assert status == 0 and output == "" and not error
    selected = query("list", "Worker", "--kinds", "parameter", source=SOURCE)
    assert selected["results"] == [] and selected["truncated"]
    assert selected["heads"][0]["symbol"] == "__stdin__.Worker"
    status, output, error = invoke("list", "Worker", source=SOURCE)
    assert status == 0 and len(output.splitlines()) == 4
    assert all(line.count("\t") == 2 for line in output.splitlines())
    assert not error
    status, output, error = invoke("list", "Worker", "--recursive", source=SOURCE)
    assert status == 0 and not error


def test_no_target_depth_is_relative_to_each_input_module(tmp_path):
    args = []
    for module in ("a", "deep.pkg.b"):
        file = tmp_path / f"{module}.py"
        file.write_text("def run(value):\n    return value\n")
        args += ["--files", f"{module}={file}"]
    result = query("list", *args, source="")
    assert {row["symbol"] for row in result["results"]} == {"a", "a.run", "deep.pkg.b", "deep.pkg.b.run"}
    assert {row["symbol"] for row in query("list", *args, "--max-depth", "0", source="")["results"]} == {
        "a",
        "deep.pkg.b",
    }


def test_batched_revision_inventories_keep_own_scope_and_source(repository):
    write(repository, "src/pkg/worker.py", SOURCE)
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "definition inventory")
    result = query("list", "v1:Worker", "HEAD:Worker", "--repo", str(repository))
    first, second = result["queries"]
    assert first["selections"][0]["scope"] == second["selections"][0]["scope"] == "pkg.worker.Worker"
    assert "pkg.worker.Worker.run" in {r["symbol"] for r in (result["results"][i] for i in first["result_indices"])}
    assert "pkg.worker.Worker.state" in {r["symbol"] for r in (result["results"][i] for i in second["result_indices"])}
    for item in (first, second):
        revision = item["input"].partition(":")[0]
        assert all(
            row["source"].startswith(revision + ":") for row in (result["results"][i] for i in item["result_indices"])
        )
        assert all(row["kind"] != "parameter" for row in (result["results"][i] for i in item["result_indices"]))


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["list", "--all"], "unrecognized arguments"),
        (["list", "--max-depth", "-1"], "must be >= 0"),
        (["show", "Worker", "--max-depth", "1"], "unrecognized arguments"),
        (["show", "Worker", "--kinds", "class"], "unrecognized arguments"),
        (["list", "--limit", "5"], "unrecognized arguments"),
        (["path", "Worker.run", "Worker.run", "--max-depth", "0"], "must be >= 1"),
    ],
)
def test_definition_options_reject_conflicts_and_wrong_actions(args, message):
    result = query(*args, source=SOURCE, status=2)
    assert message in result["error"]["message"]
