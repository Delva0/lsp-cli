"""Relationship tests assert Python semantics, including false-edge counterexamples."""

import textwrap

import pytest

from pyrel.analysis import Analysis, Summary
from pyrel.model import Source
from pyrel.queries import query


def analyze(text, **modules):
    return Analysis(
        [
            Source("example.py", "example", textwrap.dedent(text)),
            *(Source(f"{name}.py", name, textwrap.dedent(code)) for name, code in modules.items()),
        ],
    )


def calls(analysis, symbol, **options):
    return query(analysis, "callee", [symbol], **options)["results"]


def targets(analysis, symbol, **options):
    return [(event["callee"], event["target"]) for event in calls(analysis, symbol, **options)]


def test_local_class_summary_initializes_enclosing_bindings():
    a = analyze("""\
        def target(): pass
        def outer():
            alias = target
            class Local:
                value = alias()
                class Nested:
                    value = alias()
            return Local
    """)
    assert targets(a, "outer.Local") == [("alias", "example.target")]
    assert targets(a, "outer.Local.Nested") == [("alias", "example.target")]


@pytest.mark.parametrize("command", ["refs", "callers"])
def test_unvisited_local_class_after_context_limit_is_diagnosed_without_crashing(command):
    a = analyze("def target(): pass\ndef outer():\n class Local:\n  value = target()\n return Local\n")
    # Reproduce an exhausted budget without executing thousands of call paths.
    a.cache.update({("example.target", "", (), (), (i,)): Summary() for i in range(4096)})
    result = query(a, command, ["target"])
    assert any(d["kind"] == "limit" for d in result["diagnostics"])
    scope = next(d for d in result["diagnostics"] if d["kind"] == "unresolved_scope")
    assert scope["symbol"] == "example.outer.Local" and scope["line"] == 3 and scope["end_line"] == 4
    assert not any(r.get("scope") == "example.outer.Local" for r in result["results"])


@pytest.mark.parametrize("backend", ["native", "pyan"])
def test_pyan_reuse_and_c3_super_correction(backend):
    a = analyze(
        """\
        from pkg.base import A as Parent, helper as imported
        class B(Parent):
            def work(self): return super().ping()
        class C(Parent):
            def ping(self): pass
        class D(B, C):
            def run(self):
                self.ping()
                super().ping()
                super(B, self).ping()
                imported()
    """,
        **{"pkg.base": "def helper(): pass\nclass A:\n    def ping(self): pass\n"},
    )
    if backend == "pyan":
        from pyrel.backend import PyanFacts
        a = Analysis(a.sources, facts_factory=PyanFacts)
    events = calls(a, "D.run")
    assert [(e["callee"], e["target"]) for e in events] == [
        ("self.ping", "example.C.ping"),
        ("super", "builtins.super"),
        ("super().ping", "example.C.ping"),
        ("super", "builtins.super"),
        ("super(B, self).ping", "example.C.ping"),
        ("imported", "pkg.base.helper"),
    ]
    assert events[0]["binding_engine"] == ("pyan3+flow" if backend == "pyan" else "pyrel")
    assert events[2]["binding_engine"] == "pyrel"
    assert targets(a, "D.work")[-1] == ("super().ping", "example.C.ping")
    assert targets(a, "B.work")[-1] == ("super().ping", "pkg.base.A.ping")
    inheritance = query(a, "inherits", ["D"])["results"][0]
    assert inheritance["mro"] == ["example.D", "example.B", "example.C", "pkg.base.A", "builtins.object"]
    assert inheritance["mro_engine"] == ("pyan3" if backend == "pyan" else "pyrel-c3")


def test_alias_rebinding_local_function_and_shadowing():
    a = analyze("""\
        def first(): pass
        def second(): pass
        def f(unknown):
            alias = first
            alias()
            alias = second
            alias()
            alias = unknown
            alias()
            def local(): return first()
            local()
        def shadow():
            first()
            first = second
            first()
    """)
    assert targets(a, "f") == [
        ("alias", "example.first"),
        ("alias", "example.second"),
        ("alias", None),
        ("local", "example.f.local"),
    ]
    assert targets(a, "shadow") == [("first", None), ("first", "example.second")]
    assert targets(a, "f.local") == [("first", "example.first")]


def test_uses_are_not_calls_and_repeated_sites_are_preserved():
    a = analyze("""\
        def target(): pass
        def ref_only():
            alias = target
            return alias
        def caller():
            target(); target()
    """)
    assert calls(a, "ref_only") == []
    callers = query(a, "callers", ["target"])["results"]
    assert [r["caller"] for r in callers] == ["example.caller", "example.caller"]
    assert callers[0]["column"] != callers[1]["column"]
    assert {r["scope"] for r in query(a, "refs", ["target"])["results"]} >= {"example.ref_only", "example.caller"}


def test_factory_parameter_return_and_attribute_binding():
    a = analyze("""\
        class Conn:
            def send(self): pass
        def identity(x): return x
        def factory(): return Conn()
        class Adapter:
            def __init__(self): self.conn = Conn()
            def send(self): self.conn.send()
        def f():
            conn = identity(factory())
            conn.send()
            Adapter().send()
    """)
    assert targets(a, "f") == [
        ("factory", "example.factory"),
        ("identity", "example.identity"),
        ("conn.send", "example.Conn.send"),
        ("Adapter", "example.Adapter"),
        ("Adapter().send", "example.Adapter.send"),
    ]
    assert targets(a, "Adapter.send") == [("self.conn.send", "example.Conn.send")]


def test_classmethod_and_staticmethod():
    a = analyze("""\
        class Base:
            @classmethod
            def create(cls): return cls()
            @staticmethod
            def helper(): pass
            @classmethod
            def run(cls):
                cls.helper()
                return cls.create()
        class Child(Base):
            def send(self): pass
        def caller(): Child.create().send()
    """)
    assert targets(a, "caller") == [
        ("Child.create", "example.Base.create"),
        ("Child.create().send", "example.Child.send"),
    ]
    assert targets(a, "Base.run") == [("cls.helper", "example.Base.helper"), ("cls.create", "example.Base.create")]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("if flag:\n    chosen = first\nelse:\n    chosen = second\nchosen()", None),
        ("if flag:\n    chosen = first\nelse:\n    chosen = first\nchosen()", "example.first"),
        ("if flag:\n    chosen = first\nchosen()", None),
        ("chosen = first\nfor item in items:\n    chosen()\n    chosen = second", None),
    ],
)
def test_branches_and_loop_bindings(body, expected):
    a = analyze("def first(): pass\ndef second(): pass\ndef f(flag, items):\n" + textwrap.indent(body, "    "))
    assert calls(a, "f")[-1]["target"] == expected


def test_cross_input_relative_imports_module_alias_and_reexport():
    a = Analysis(
        [
            Source(
                "pkg/use.py",
                "pkg.use",
                "from . import exported as run\nimport pkg.base as b\ndef f():\n run()\n b.target()\n",
            ),
            Source("pkg/__init__.py", "pkg", "from .base import target as exported\n", is_package=True),
            Source("pkg/base.py", "pkg.base", "def target(): pass\n"),
        ],
    )
    assert targets(a, "f") == [("run", "pkg.base.target"), ("b.target", "pkg.base.target")]


def test_unknown_external_decorator_and_missing_base_never_guess():
    a = analyze("""\
        from absent import Outside
        def decorate(f): return unknown
        @decorate
        def wrapped(): pass
        class Known:
            def send(self): pass
        class Child(Outside, Known):
            def f(self, other):
                other.send()
                self.send()
                super().send()
                wrapped()
    """)
    events = calls(a, "Child.f")
    assert all(e["target"] is None for e in events if e["callee"] != "super")
    assert query(a, "inherits", ["Child"])["results"][0]["resolution"] == "unresolved"


def test_invalid_c3_does_not_use_pyans_dfs_fallback():
    a = analyze("""\
        class A: pass
        class B: pass
        class X(A, B): pass
        class Y(B, A): pass
        class Z(X, Y):
            def f(self): super().missing()
    """)
    result = query(a, "inherits", ["Z"])["results"][0]
    assert result["resolution"] == "unresolved"
    assert "inconsistent C3" in result["reason"]
    assert calls(a, "Z.f")[-1]["target"] is None


def test_path_uses_concrete_receiver_and_parameters():
    a = analyze("""\
        class Base:
            def work(self): self.finish()
        class Child(Base):
            def finish(self): pass
        def helper(x): x.work()
        def start(): helper(Child())
    """)
    result = query(a, "path", ["start", "Child.finish"])
    assert result["status"] == "found"
    assert [r["target"] for r in result["results"]] == ["example.helper", "example.Base.work", "example.Child.finish"]
    assert all(r["source"] == "example.py" and r["end_column"] > 0 for r in result["results"])


def test_unknown_path_explains_boundary_and_limits():
    a = analyze("def end(): pass\ndef start(x): x.send()\n")
    result = query(a, "path", ["start", "end"])
    assert result["status"] == "no_resolved_path"
    assert result["unresolved_frontier"][0]["callee"] == "x.send"


def test_refs_keep_receiver_binding_and_aliases():
    a = analyze("""\
        def f(conn, other):
            alias = conn
            conn.cert_reqs = 0
            print(alias.cert_reqs)
            print(other.cert_reqs)
    """)
    result = query(a, "refs", ["conn.cert_reqs"])
    assert [r["expression"] for r in result["results"]] == ["conn.cert_reqs", "alias.cert_reqs"]


def test_trace_context_evaluation_order_and_unicode_spans():
    a = analyze("""\
        def get(): pass
        def f(conn, flag):
            名称 = get()
            if flag:
                conn.cert_reqs = 0
            else:
                conn[0] = get()
            try:
                with get() as context:
                    for item in context:
                        conn.send(get())
            except ValueError:
                raise RuntimeError()
            finally:
                conn.closed = True
            return 名称
    """)
    events = query(a, "trace", ["f"])["results"]
    assert [e["kind"] for e in events[:5]] == ["CALL", "ASSIGN", "ATTR_WRITE", "CALL", "SUBSCRIPT_WRITE"]
    assert events[0]["column"] == 9
    assert {e["kind"] for e in events} == {"CALL", "ASSIGN", "ATTR_WRITE", "SUBSCRIPT_WRITE", "RAISE", "RETURN"}
    send = next(e for e in events if e.get("callee") == "conn.send")
    assert [c["kind"] for c in send["context"]] == ["try", "with", "loop"]
    nested = events[events.index(send) - 1]
    assert nested["callee"] == "get"
    for event in events:
        assert all(k in event for k in ("source", "line", "column", "end_line", "end_column"))
    scopes = query(a, "where", line=5)["results"]
    assert [scope["symbol"] for scope in scopes] == ["example", "example.f"]


def test_nested_body_not_in_outer_trace_and_dead_calls_not_paths():
    a = analyze("""\
        def target(): pass
        def f():
            def inner(): target()
            return None
            target()
    """)
    events = calls(a, "f")
    assert len(events) == 1 and events[0]["unreachable"]
    assert query(a, "path", ["f", "target"])["status"] == "no_resolved_path"


def test_recursion_terminates_without_fabricating_returns():
    a = analyze("def recurse(): return recurse()\ndef f(): recurse().send()\n")
    assert targets(a, "f")[-1] == ("recurse().send", None)


def test_no_analyzed_code_is_executed(tmp_path):
    destination = tmp_path / "must-not-exist"
    a = analyze(f"open({str(destination)!r}, 'w').write('bad')\ndef f(): pass\n")
    assert query(a, "outline")["results"]
    assert not destination.exists()


def test_constructors_bind_fields_and_effects_without_allocation_aliasing():
    a = analyze("""\
        class A:
            def run(self): pass
        class B:
            def run(self): pass
        class Holder:
            def __init__(self, value): self.value = value
            def invoke(self): self.value.run()
        def make(value): return Holder(value)
        def start():
            a = make(A())
            b = make(B())
            a.value.run()
            b.value.run()
            a.invoke()
    """)
    assert targets(a, "start")[-3:] == [
        ("a.value.run", "example.A.run"),
        ("b.value.run", "example.B.run"),
        ("a.invoke", "example.Holder.invoke"),
    ]
    found = query(a, "path", ["start", "A.run"])
    assert found["status"] == "found"
    assert any(r["caller"] == "example.Holder.invoke" for r in query(a, "callers", ["A.run"])["results"])


def test_distinct_returned_closures_and_late_bound_free_variables():
    a = analyze("""\
        def first(): pass
        def second(): pass
        def make(callback):
            def inner(): return callback()
            return inner
        def start():
            a = make(first)
            b = make(second)
            a()
            b()
        def outer():
            callback = first
            def inner(): callback()
            callback = second
            inner()
    """)
    assert query(a, "path", ["start", "first"])["status"] == "found"
    assert query(a, "path", ["start", "second"])["status"] == "found"
    assert targets(a, "outer.inner") == [("callback", "example.second")]


def test_actual_default_arguments_and_lexical_comprehension_scope():
    a = analyze("""\
        def original(): pass
        def other(): pass
        cb = original
        def with_default(callback=cb): callback()
        cb = other
        def start():
            with_default()
            [cb() for cb in [original]]
            cb()
    """)
    assert targets(a, "start")[-1] == ("cb", "example.other")
    assert query(a, "path", ["start", "original"])["status"] == "found"
    assert calls(a, "with_default")[0]["target"] is None


def test_await_and_context_manager_return_types():
    a = analyze("""\
        class Conn:
            def send(self): pass
        class Manager:
            def __enter__(self): return Conn()
            def __exit__(self, *args): pass
        async def factory(): return Conn()
        async def start():
            conn = await factory()
            conn.send()
            factory().send()
            with Manager() as entered:
                entered.send()
    """)
    assert targets(a, "start") == [
        ("factory", "example.factory"),
        ("conn.send", "example.Conn.send"),
        ("factory", "example.factory"),
        ("factory().send", None),
        ("Manager", "example.Manager"),
        ("entered.send", "example.Conn.send"),
    ]


def test_global_writes_unknown_star_and_dynamic_attribute_access():
    a = analyze("""\
        def original(): pass
        def change(value):
            global original
            original = value
        class Dynamic:
            def __getattribute__(self, name): return unknown
            def run(self): pass
        def start():
            original()
            Dynamic().run()
    """)
    assert targets(a, "start") == [("original", None), ("Dynamic", "example.Dynamic"), ("Dynamic().run", None)]
    b = analyze("def f(): pass\nfrom unavailable import *\ndef start(): f()\n")
    assert targets(b, "start") == [("f", None)]


def test_try_intermediate_binding_and_loop_break_do_not_prove_wrong_target():
    a = analyze("""\
        def first(): pass
        def second(): pass
        def f(flag, items):
            cb = first
            try:
                cb = second
                unknown()
                cb = first
            except Exception:
                cb()
            cb = first
            for item in items:
                if flag:
                    cb = second
                    break
                cb = first
            cb()
    """)
    assert all(e["target"] is None for e in calls(a, "f"))


def test_bare_annotation_is_not_a_write_and_rebound_self_invalidates_super():
    a = analyze("""\
        class Base:
            def run(self): pass
        class Child(Base):
            def f(self, other):
                self = other
                super().run()
        def start():
            item = Base()
            item: Child
            item.run()
    """)
    assert targets(a, "start")[-1] == ("item.run", "example.Base.run")
    assert calls(a, "Child.f")[-1]["target"] is None


def test_constant_dead_branch_not_a_path():
    a = analyze("def target(): pass\ndef start():\n if False:\n  target()\n")
    assert calls(a, "start")[0]["unreachable"]
    assert query(a, "path", ["start", "target"])["status"] == "no_resolved_path"


def test_nested_class_and_module_definition_order():
    a = analyze("""\
        target()
        def target(): pass
        class Base:
            def work(self): target()
        def make():
            class Local(Base):
                def run(self): self.work()
            return Local()
    """)
    assert targets(a, "example") == [("target", None)]
    assert targets(a, "make.Local.run") == [("self.work", "example.Base.work")]
    assert query(a, "inherits", ["make.Local"])["results"][0]["mro"] == [
        "example.make.Local",
        "example.Base",
        "builtins.object",
    ]


def test_unknown_mutator_invalidates_instance_bindings():
    a = analyze("""\
        class C:
            def run(self): pass
        def start(mutator):
            item = C()
            item.run()
            mutator(item)
            item.run()
    """)
    assert targets(a, "start") == [
        ("C", "example.C"),
        ("item.run", "example.C.run"),
        ("mutator", None),
        ("item.run", None),
    ]


def test_external_import_reference_is_findable_by_original_name():
    a = analyze("from absent import work as run\ndef start(): run()\n")
    result = query(a, "refs", ["absent.work"])
    assert [r["role"] for r in result["results"]] == ["import", "read"]
    assert all(r["resolution"] == "unresolved" and r["import_target"] == "absent.work" for r in result["results"])


def test_augmented_write_evaluates_receiver_once_before_rhs():
    a = analyze("def obj(): return unknown\ndef rhs(): return 1\ndef start(): obj().value += rhs()\n")
    assert targets(a, "start") == [("obj", "example.obj"), ("rhs", "example.rhs")]
    assert query(a, "trace", ["start"])["results"][-1]["kind"] == "ATTR_WRITE"


def test_no_edge_to_unbound_export_and_path_to_constructor_symbol():
    a = analyze(
        "from dep import never\nclass C: pass\ndef start():\n never()\n C()\n", dep="if False:\n def never(): pass\n",
    )
    assert targets(a, "start")[0] == ("never", None)
    assert query(a, "path", ["start", "C"])["status"] == "found"


def test_mutable_constructor_field_is_not_assumed_for_generic_receiver():
    a = analyze("""\
        class A:
            def send(self): pass
        class B:
            def send(self): pass
        class Holder:
            def __init__(self): self.conn = A()
            def reset(self): self.conn = B()
            def run(self): self.conn.send()
        def start():
            h = Holder()
            h.reset()
            h.run()
    """)
    assert targets(a, "Holder.run") == [("self.conn.send", None)]
    assert query(a, "path", ["start", "B.send"])["status"] == "found"


def test_class_method_assignment_is_a_binding_not_a_name_match():
    a = analyze("""\
        def replacement(self): pass
        class C:
            def method(self): pass
        C.method = replacement
        def start(): C().method()
    """)
    assert targets(a, "start")[-1] == ("C().method", "example.replacement")


def test_unknown_return_does_not_promote_factory_to_receiver_or_callee_candidate():
    a = analyze("from absent import factory\ndef start():\n conn = factory()\n conn.urlopen()\n")
    event = calls(a, "start")[-1]
    assert event["resolution"] == "unresolved" and event["target"] is None
    assert not event.get("candidates")


def test_container_lookup_respects_overwrite_and_unknown_unpacking():
    a = analyze("""\
        class A:
            def send(self): pass
        class B:
            def send(self): pass
        def start(extra):
            {'key': A(), 'key': B()}['key'].send()
            {'key': A(), **extra}['key'].send()
            {A(), B()}[0].send()
    """)
    events = [e for e in calls(a, "start") if e["callee"].endswith(".send")]
    assert [e["target"] for e in events] == ["example.B.send", None, None]
