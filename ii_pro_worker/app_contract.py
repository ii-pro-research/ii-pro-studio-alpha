"""The operator-owned black-box contract for the first SQLite task manager."""

import ast

if __package__:
    from .wire import exact, fingerprint, integer, text
else:
    from wire import exact, fingerprint, integer, text


PROFILE = "task-manager-wsgi-v1"
FILTER_PROFILE = "task-manager-filter-wsgi-v1"
SORT_PROFILE = "wsgi-title-sort-v1"
CASES = ("home", "create", "edit_and_complete", "restart")
FILTER_CASES = (*CASES, "filter_open", "filter_completed", "filter_all")
PROFILES = {PROFILE: CASES, FILTER_PROFILE: FILTER_CASES,
            SORT_PROFILE: (*FILTER_CASES, "sort_title_json", "sort_title_html",
                           "sort_with_status", "sort_default_and_unknown")}


def app_package(code, profile=PROFILE):
    value = {"profile": profile, "code": code, "cases": list(PROFILES[profile])}
    value["digest"] = fingerprint(value)
    return validate_app_package(value)


def validate_app_package(value):
    exact(value, {"profile", "code", "cases", "digest"})
    text(value["code"], 4400)
    if (type(value["profile"]) is not str or value["profile"] not in PROFILES
            or value["cases"] != list(PROFILES[value["profile"]])
            or value["digest"] != fingerprint({k: v for k, v in value.items() if k != "digest"})):
        raise ValueError("Application package scope differs")
    tree = ast.parse(value["code"])
    if not any(isinstance(node, ast.FunctionDef) and node.name == "create_app" for node in tree.body):
        raise ValueError("Expected create_app(db_path)")
    return value


def validate_app_result(value, package):
    exact(value, {"method", "package_digest", "cases", "passed", "total"})
    profile = package["profile"]
    cases = PROFILES[profile]
    if value["method"] != profile or value["package_digest"] != package["digest"] or value["total"] != len(cases):
        raise ValueError("Application result scope differs")
    if type(value["cases"]) is not list or len(value["cases"]) != len(cases):
        raise ValueError("Application cases missing")
    for case, name in zip(value["cases"], cases, strict=True):
        exact(case, {"id", "status", "diagnostic"})
        if case["id"] != name or (case["status"], case["diagnostic"]) not in {
            ("passed", "ok"), ("failed", "response_mismatch"), ("error", "application_error"),
        }:
            raise ValueError("Invalid application case")
    integer(value["passed"], 0, len(cases))
    if value["passed"] != sum(c["status"] == "passed" for c in value["cases"]):
        raise ValueError("Invalid application total")
    return value
