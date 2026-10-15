"""Conservative receiver-effect checks before accepting a native call edge."""

import ast

SPECIAL = {"__class__", "__dict__", "__getattribute__", "__getattr__", "__setattr__", "__delattr__", "__new__"}


def preserves_receiver(ws, source, function, receiver, attribute, order, *, before=None, declaration=None,
                       active=frozenset()):
    """Prove a small static subset; receiver escape or unknown effects require flow.

    Follow calls to methods in the proven MRO. Arbitrary callbacks, aliases,
    reassignment and dynamic writes are deliberately outside this proof.
    """
    key = id(function), receiver, attribute, tuple(cls.name for cls in order), before, id(declaration)
    if key in active:
        return False
    cache = getattr(ws, "receiver_effects", None)
    if cache is None:
        ws.receiver_effects = cache = {}
    if key in cache:
        return cache[key]
    catalog = ws.catalog
    active = active | {key}
    declarations = {}
    for cls in reversed(order):
        for entry in catalog.entries:
            if entry.parent == cls.name:
                declarations[entry.name.rsplit(".", 1)[-1]] = entry

    def method_safe(entry):
        if entry is None or entry.kind != "method" or entry.decorators:
            return False
        args = [*entry.node.args.posonlyargs, *entry.node.args.args]
        return bool(args) and preserves_receiver(
            ws, entry.source, entry.node, args[0].arg, attribute, order, active=active,
        )

    def safe():
        for node in ast.walk(function):
            if before is not None and hasattr(node, "lineno") and (node.lineno, source.span(node)["column"]) >= before:
                continue
            if isinstance(node, (ast.Global, ast.Nonlocal)) and receiver in node.names:
                return False
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "super":
                member = catalog.parents.get(id(node))
                use = catalog.parents.get(id(member))
                if not (isinstance(member, ast.Attribute) and member.value is node
                        and isinstance(use, ast.Call) and use.func is member):
                    return False
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Call)):
                expression = node.func.value
                if isinstance(expression.func, ast.Name) and expression.func.id == "super":
                    scope = catalog.node_scopes[id(node)]
                    if (catalog.lookup(scope, "super") or source.guards["star"] or expression.keywords
                            or not scope.parent or scope.parent.kind != "class"):
                        return False
                    if expression.args and not (
                        len(expression.args) == 2 and isinstance(expression.args[0], ast.Name)
                        and isinstance(expression.args[1], ast.Name) and expression.args[1].id == receiver
                        and (declared := catalog.lookup(scope, expression.args[0].id))
                        and declared.name == scope.parent.name
                    ):
                        return False
                    names = [cls.name for cls in order]
                    if scope.parent.name not in names:
                        return False
                    inherited = next((catalog.select(name + "." + node.func.attr)
                                      for name in names[names.index(scope.parent.name) + 1:]
                                      if catalog.select(name + "." + node.func.attr)), [])
                    if inherited:
                        if len(inherited) != 1 or not method_safe(inherited[0]):
                            return False
                    elif node.func.attr != "__init__":
                        return False
            if not isinstance(node, ast.Name) or node.id != receiver:
                continue
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                if isinstance(node.ctx, ast.Store) and catalog.parents.get(id(node)) is declaration:
                    continue
                return False
            parent = catalog.parents.get(id(node))
            if isinstance(parent, ast.Attribute) and parent.value is node:
                if parent.attr in SPECIAL:
                    return False
                if isinstance(parent.ctx, (ast.Store, ast.Del)):
                    if parent.attr in SPECIAL | {attribute}:
                        return False
                    continue
                entry = declarations.get(parent.attr)
                call = catalog.parents.get(id(parent))
                if isinstance(call, ast.Call) and call.func is parent:
                    if not method_safe(entry):
                        return False
                elif entry and entry.kind == "method":
                    # A bound method escapes, or a descriptor could return self.
                    return False
                continue
            if isinstance(parent, ast.Call) and node in parent.args:
                # Explicit Base.__init__(self, ...) is common in Python 2-era
                # source. Prove the base binding before inspecting its effects.
                func = parent.func
                if isinstance(func, ast.Attribute) and parent.args[0] is node:
                    found = ws.definition(source, source.span(func.value))
                    if len(found) == 1 and found[0].kind == "class" and any(
                        cls.name == found[0].name for cls in order
                    ):
                        methods = catalog.select(found[0].name + "." + func.attr)
                        if len(methods) == 1 and method_safe(methods[0]):
                            continue
                # A proven builtin super expression doesn't let the receiver escape.
                if isinstance(func, ast.Name) and func.id == "super":
                    scope = catalog.node_scopes[id(node)]
                    if not catalog.lookup(scope, "super") and not source.guards["star"]:
                        continue
            return False
        return True

    cache[key] = safe()
    return cache[key]
