"""Stdlib-only narrow contract. Validation parses candidate code, never runs it.

This finite language profile is defense in depth, not an OS sandbox.
"""
import ast
import hashlib
import json
import re


class PackageError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def code_sha(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise PackageError("unexpected_fields")


def integer(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise PackageError("integer_budget")


def name(value):
    if type(value) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", value) or "__" in value:
        raise PackageError("name_contract")


def validate_code(code):
    if type(code) is not str or len(code.encode("utf-8")) > 4096 or "\0" in code:
        raise PackageError("code_budget")
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise PackageError("syntax") from exc
    if len(list(ast.walk(tree))) > 256:
        raise PackageError("ast_budget")
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise PackageError("single_function")
    fn = tree.body[0]
    if (fn.name != "sum_positive" or [a.arg for a in fn.args.args] != ["values"]
            or fn.args.posonlyargs or fn.args.kwonlyargs or fn.args.vararg or fn.args.kwarg
            or fn.args.defaults or fn.decorator_list or fn.returns or fn.type_params
            or any(a.annotation for a in fn.args.args)):
        raise PackageError("signature")

    def expr(node, scope, depth=0):
        if depth > 24:
            raise PackageError("expression_depth")
        def child(value):
            return expr(value, scope, depth + 1)
        if isinstance(node, ast.Name):
            name(node.id)
            if node.id not in scope:
                raise PackageError("unknown_name")
            return scope[node.id]
        if isinstance(node, ast.Constant):
            if type(node.value) is bool:
                return "bool"
            integer(node.value, -10000, 10000)
            return "int"
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.FloorDiv, ast.Mod)):
            if child(node.left) == child(node.right) == "int":
                return "int"
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
            if child(node.operand) in {"int", "bool"}:
                return "bool" if isinstance(node.op, ast.Not) else "int"
        elif isinstance(node, ast.Compare):
            if all(isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for op in node.ops):
                if all(child(n) == "int" for n in [node.left, *node.comparators]):
                    return "bool"
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            types = [child(n) for n in node.values]
            if len(set(types)) == 1 and types[0] in {"bool", "int"}:
                return types[0]
        elif isinstance(node, ast.IfExp):
            if child(node.test) in {"bool", "int"} and child(node.body) == child(node.orelse) == "int":
                return "int"
        elif isinstance(node, (ast.GeneratorExp, ast.ListComp)):
            if len(node.generators) != 1:
                raise PackageError("comprehension_budget")
            gen = node.generators[0]
            if (gen.is_async or not isinstance(gen.target, ast.Name) or gen.target.id == "values"
                    or not isinstance(gen.iter, ast.Name) or gen.iter.id != "values"):
                raise PackageError("bounded_iterator")
            name(gen.target.id)
            local = {**scope, gen.target.id: "int"}
            if expr(node.elt, local, depth + 1) == "int" and all(expr(n, local, depth + 1) in {"bool", "int"} for n in gen.ifs):
                return "int_list"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords and len(node.args) == 1:
            kind = node.func.id
            arg = child(node.args[0])
            if (kind in {"sum", "len", "min", "max"} and arg == "int_list") or (kind == "abs" and arg == "int"):
                return "int"
        raise PackageError("unsupported_expression")

    def statements(body, scope, loop_depth=0):
        for node in body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
                name(target)
                kind = expr(node.value, scope)
                if target == "values" or kind not in {"int", "bool"}:
                    raise PackageError("scalar_assignment_only")
                scope[target] = kind
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and isinstance(node.op, (ast.Add, ast.Sub)):
                if node.target.id == "values" or scope.get(node.target.id) != "int" or expr(node.value, scope) != "int":
                    raise PackageError("scalar_assignment_only")
            elif isinstance(node, ast.For):
                if (loop_depth >= 2 or not isinstance(node.target, ast.Name) or node.target.id == "values"
                        or not isinstance(node.iter, ast.Name) or node.iter.id != "values" or node.orelse):
                    raise PackageError("bounded_iterator")
                name(node.target.id)
                scope[node.target.id] = "int"
                statements(node.body, scope, loop_depth + 1)
            elif isinstance(node, ast.If):
                if expr(node.test, scope) not in {"bool", "int"}:
                    raise PackageError("condition")
                statements(node.body, scope, loop_depth)
                statements(node.orelse, scope, loop_depth)
            elif isinstance(node, ast.Return):
                if expr(node.value, scope) not in {"int", "bool"}:
                    raise PackageError("scalar_return_only")
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and type(node.value.value) is str and len(node.value.value) <= 512:
                pass  # bounded docstring, never executable I/O
            else:
                raise PackageError("unsupported_statement")
    statements(fn.body, {"values": "int_list"})
    return tree


def pricing_code(code):
    if type(code) is not str or len(code.encode("utf-8")) > 1024 or "\0" in code:
        raise PackageError("code_budget")
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise PackageError("syntax") from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise PackageError("single_function")
    fn = tree.body[0]
    if (fn.name != "discounted" or ast.dump(fn.args) != ast.dump(
            ast.parse("def discounted(price, percent): pass").body[0].args)
            or fn.decorator_list or fn.returns or fn.type_params or
            len(fn.body) != 1 or not isinstance(fn.body[0], ast.Return)):
        raise PackageError("signature_or_body")

    def arithmetic(node, depth=0):
        if depth > 6:
            raise PackageError("expression_depth")
        if isinstance(node, ast.Name) and node.id in {"price", "percent"}:
            return
        if isinstance(node, ast.Constant):
            integer(node.value, -10000, 10000)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            arithmetic(node.left, depth + 1)
            arithmetic(node.right, depth + 1)
        else:
            raise PackageError("unsupported_expression")

    arithmetic(fn.body[0].value)


def tests_digest(cases, function="sum_positive"):
    return sha({"profile": "integer-list-v1" if function == "sum_positive" else "pricing-arithmetic-v1",
                "function": function, "cases": cases})


def validate_package(package):
    exact(package, {"version", "id", "function", "code", "source_sha256", "suite_digest", "cases", "digest"})
    integer(package["version"], 1, 1)
    if type(package["id"]) is not str or not re.fullmatch(r"[a-f0-9]{32}", package["id"]):
        raise PackageError("package_identity")
    function = package["function"]
    if type(package["code"]) is not str or function not in ("sum_positive", "discounted") or package["source_sha256"] != code_sha(package["code"]):
        raise PackageError("source_identity")
    (validate_code if function == "sum_positive" else pricing_code)(package["code"])
    cases = package["cases"]
    if type(cases) is not list or not 2 <= len(cases) <= 16:
        raise PackageError("test_budget")
    seen = set()
    for case in cases:
        exact(case, {"id", "input", "expected"})
        name(case["id"])
        if case["id"] in seen or type(case["input"]) is not list or len(case["input"]) > 32:
            raise PackageError("case_identity_or_budget")
        seen.add(case["id"])
        for value in case["input"]:
            integer(value, -10000, 10000)
        if function == "discounted" and (len(case["input"]) != 2 or not 0 <= case["input"][1] <= 100):
            raise PackageError("pricing_input")
        integer(case["expected"], -1000000, 1000000)
    if package["suite_digest"] != tests_digest(cases, function) or package["digest"] != sha({k: v for k, v in package.items() if k != "digest"}):
        raise PackageError("package_digest")
    if len(canonical(package).encode()) > 10000:
        raise PackageError("package_budget")
    return package


def make_package(code, cases, package_id, *, function="sum_positive"):
    package = {"version": 1, "id": package_id, "function": function, "code": code,
               "source_sha256": code_sha(code), "suite_digest": tests_digest(cases, function), "cases": cases}
    package["digest"] = sha(package)
    return validate_package(package)


def validate_result(result, package):
    exact(result, {"method", "package_id", "package_digest", "source_sha256", "suite_digest", "cases", "passed", "total", "resources"})
    method = "integer-tests-v1" if package["function"] == "sum_positive" else "pricing-arithmetic-tests-v1"
    if (result["method"] != method or result["package_id"] != package["id"]
            or result["package_digest"] != package["digest"] or result["source_sha256"] != package["source_sha256"]
            or result["suite_digest"] != package["suite_digest"]):
        raise PackageError("result_scope")
    if type(result["cases"]) is not list or len(result["cases"]) != len(package["cases"]):
        raise PackageError("result_cases")
    for actual, expected in zip(result["cases"], package["cases"], strict=True):
        exact(actual, {"id", "status", "diagnostic"})
        if (actual["id"] != expected["id"] or actual["status"] not in ("passed", "failed", "error")
                or actual["diagnostic"] not in ("ok", "wrong_value", "candidate_exception")):
            raise PackageError("result_cases")
        if {"passed": "ok", "failed": "wrong_value", "error": "candidate_exception"}[actual["status"]] != actual["diagnostic"]:
            raise PackageError("result_contradiction")
    integer(result["total"], len(package["cases"]), len(package["cases"]))
    integer(result["passed"], 0, result["total"])
    if result["passed"] != sum(c["status"] == "passed" for c in result["cases"]):
        raise PackageError("result_count")
    exact(result["resources"], {"memory_peak", "cpu_usage_usec", "pids_current"})
    integer(result["resources"]["memory_peak"], 1, 134217728)
    integer(result["resources"]["cpu_usage_usec"], 0, 10000000)
    integer(result["resources"]["pids_current"], 1, 16)
    return result
