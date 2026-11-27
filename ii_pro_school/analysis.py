"""Bounded AST inspection. No eval, exec, imports or running candidate tests."""
import ast
import re


def analyze_answer(answer: str, lesson: dict) -> dict:
    scores = {k: 0 for k in ("syntax", "signature", "single_function_scope", "static_evidence")}
    findings = []
    blocks = re.findall(r"```(?:python|py)\s*\n(.*?)```", answer, re.DOTALL)
    if len(answer.encode("utf-8")) > 65536 or len(blocks) != 1:
        findings.append("Expected one bounded Python code fence")
    else:
        try:
            tree = ast.parse(blocks[0])
            nodes = list(ast.walk(tree))
            if len(nodes) > 5000:
                raise ValueError("AST budget exceeded")
            scores["syntax"] = lesson["rubric"]["syntax"]
            fn = tree.body[0] if len(tree.body) == 1 else None
            if (isinstance(fn, ast.FunctionDef) and fn.name == lesson["function"]
                    and [a.arg for a in fn.args.args] == lesson["arguments"]
                    and not (fn.args.posonlyargs or fn.args.kwonlyargs or fn.args.vararg or fn.args.kwarg
                             or fn.args.defaults or fn.decorator_list or fn.returns)
                    and not any(a.annotation for a in fn.args.args)):
                scores["signature"] = lesson["rubric"]["signature"]
            else:
                findings.append("Signature differs from the lesson contract")
            forbidden = (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.ClassDef, ast.AsyncFunctionDef)
            functions = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in nodes)
            obvious_io = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                             and n.func.id in {"open", "exec", "eval", "__import__", "compile", "input",
                                               "globals", "locals", "getattr", "setattr", "delattr", "breakpoint"}
                             for n in nodes)
            if isinstance(fn, ast.FunctionDef) and functions == 1 and not obvious_io and not any(isinstance(n, forbidden) for n in nodes):
                scores["single_function_scope"] = lesson["rubric"]["single_function_scope"]
            else:
                findings.append("Expected only one function without imports or additional definitions")
            # Evidence, never a behavioral proof or a safety certification.
            evidence = False
            if lesson["id"] == "bug-positive-sum":
                evidence = any(isinstance(n, ast.AugAssign) and isinstance(n.op, ast.Add)
                               and isinstance(n.value, ast.Name) and n.value.id == "value" for n in nodes)
            elif lesson["id"] == "write-clamp":
                raised = {ast.unparse(n.exc) for n in nodes if isinstance(n, ast.Raise) and n.exc}
                evidence = any("TypeError" in x for x in raised) and any("ValueError" in x for x in raised)
            elif lesson["id"] == "improve-unique":
                evidence = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "set" for n in nodes)
            if evidence and scores["signature"] and scores["single_function_scope"]:
                scores["static_evidence"] = lesson["rubric"]["static_evidence"]
                findings.append("Recognized a relevant AST pattern; behavior remains untested")
            else:
                findings.append("No supported lesson-specific AST pattern established")
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            findings.append("Candidate could not be parsed within static limits")
    return {"method": "ast_only_v1", "status": "static_only", "scores": scores,
            "score_awarded": sum(scores.values()), "score_available": 45, "score_total": 100,
            "functional_status": "not_executed", "exam_status": "not_taken",
            "mentor_status": "not_connected", "verified_knowledge": False, "findings": findings}
