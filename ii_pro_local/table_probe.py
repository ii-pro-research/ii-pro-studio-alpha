"""Fixed synthetic dataset for a two-step Local tool probe."""

import json

from ii_pro_core.errors import ValidationError
from ii_pro_core.security import canonical, digest

DATASET = "table-probe-v1"
JOB = digest([DATASET, "study"])[:32]
TASK = "Select active=1 and score>=7 (exclude NULL). Order zone ASCII case-insensitive ASC, score DESC, then id ASC. Return ordered IDs."
REQUIREMENTS = "Propose filters and keys using table_check. After seeing the result, give the exact ID list, validate the rules and cite step 1."
COLUMNS = ("id", "active", "score", "zone")
ROWS = (
    (45, 1, 7, "north"), (13, 1, 9, "West"), (62, 1, 9, "west"),
    (28, 1, 10, "NORTH"), (9, 0, 12, "north"), (77, 1, None, "west"),
    (31, 1, 6, "north"),
)
DATASET_SHA256 = digest([DATASET, COLUMNS, ROWS])
MAX_ROWS = 12

GROUNDED_DATASET = "table-probe-v2"
GROUNDED_JOB = digest([GROUNDED_DATASET, "study"])[:32]
GROUNDED_TASK = "Keep active=0 and score>=6, excluding NULL. Sort by zone ASCII case-insensitive DESC, score ASC, id DESC. Predict ordered IDs."
GROUNDED_REQUIREMENTS = ("Give predicted IDs. One short sentence per other text field: SQL/shared GET risk, "
                         "evidence and a distinguishing check. Then stop.")
GROUNDED_ROWS = (
    (84, 0, 6, "East"), (72, 0, 8, "west"), (19, 0, 8, "WEST"),
    (64, 0, 6, "west"), (9, 1, 6, "West"), (55, 0, None, "West"),
    (47, 0, 5, "west"), (23, 0, 6, "east"),
)
DATASETS = {DATASET: ROWS, GROUNDED_DATASET: GROUNDED_ROWS}
JOBS = {JOB: (DATASET, TASK, REQUIREMENTS),
        GROUNDED_JOB: (GROUNDED_DATASET, GROUNDED_TASK, GROUNDED_REQUIREMENTS)}


def dataset_digest(dataset):
    return digest([dataset, COLUMNS, DATASETS[dataset]])


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def arguments_schema(dataset=DATASET):
    return {
        "dataset": {"type": "string", "enum": [dataset]},
        "filters": {"type": "array", "maxItems": 3,
                    "items": _object({"field": {"type": "string", "enum": ["active", "score"]},
                                      "op": {"type": "string", "enum": ["eq", "gte"]},
                                      "value": {"type": "integer", "minimum": 0, "maximum": 100}})},
        "sort": {"type": "array", "minItems": 1, "maxItems": 3,
                 "items": _object({"field": {"type": "string", "enum": ["zone", "score", "id"]},
                                   "direction": {"type": "string", "enum": ["asc", "desc"]}})},
    }


def validate(args, *, dataset=DATASET):
    if (type(args) is not dict or set(args) != {"dataset", "filters", "sort"}
            or args["dataset"] != dataset or type(args["filters"]) is not list or len(args["filters"]) > 3
            or type(args["sort"]) is not list or not 1 <= len(args["sort"]) <= 3):
        raise ValidationError("Invalid bounded table parameters")
    for condition in args["filters"]:
        if (type(condition) is not dict or set(condition) != {"field", "op", "value"}
                or condition["field"] not in ("active", "score") or condition["op"] not in ("eq", "gte")
                or type(condition["value"]) is not int or not 0 <= condition["value"] <= 100):
            raise ValidationError("Invalid bounded table filter")
    for key in args["sort"]:
        if (type(key) is not dict or set(key) != {"field", "direction"}
                or key["field"] not in ("zone", "score", "id") or key["direction"] not in ("asc", "desc")):
            raise ValidationError("Invalid bounded table sort key")
    if len({key["field"] for key in args["sort"]}) != len(args["sort"]):
        raise ValidationError("Repeated bounded table sort key")


def evaluate(args, *, rows=None, dataset=DATASET):
    validate(args, dataset=dataset)
    rows = DATASETS[dataset] if rows is None else rows
    if type(rows) is not tuple or len(rows) > MAX_ROWS:
        raise ValidationError("Bounded table row limit exceeded")
    for row in rows:
        if (type(row) is not tuple or len(row) != 4 or type(row[0]) is not int or not 1 <= row[0] <= 999
                or type(row[1]) is not int or row[1] not in (0, 1)
                or (row[2] is not None and (type(row[2]) is not int or not 0 <= row[2] <= 100))
                or type(row[3]) is not str or not 1 <= len(row[3]) <= 12 or not row[3].isascii()):
            raise ValidationError("Invalid bounded table row")
    if len({row[0] for row in rows}) != len(rows):
        raise ValidationError("Duplicate table row ID")
    selected = list(rows)
    for condition in args["filters"]:
        column, value = COLUMNS.index(condition["field"]), condition["value"]
        if condition["op"] == "eq":
            selected = [row for row in selected if row[column] == value]
        else:
            selected = [row for row in selected if row[column] is not None and row[column] >= value]
    for order in reversed(args["sort"]):
        column = COLUMNS.index(order["field"])
        present = [row for row in selected if row[column] is not None]
        present.sort(key=lambda row: row[column].lower() if column == 3 else row[column],
                     reverse=order["direction"] == "desc")
        selected = present + [row for row in selected if row[column] is None]
    return {"dataset": dataset, "dataset_sha256": digest([dataset, COLUMNS, rows]),
            "count": len(selected), "ids": [row[0] for row in selected]}


def source(dataset=DATASET):
    return canonical({"dataset": dataset, "columns": COLUMNS, "rows": DATASETS[dataset]})


def assess_finish(finish, observation):
    """Compare the model's explicit observation claim without modifying its answer."""
    try:
        ids = json.loads(finish["prediction"])
    except (ValueError, RecursionError):
        ids = None
    proposed = ids if type(ids) is list and all(type(item) is int for item in ids) else None
    matches = (proposed == observation["ids"] and len(proposed) == observation["count"])
    interpretation = finish["comparison"].strip()
    if not matches:
        status = "observation_mismatch"
    elif interpretation == "confirmed":
        status = "observation_matched"
    elif interpretation.startswith("revise:") and interpretation[len("revise:"):].strip():
        status = "revision_requested"
    else:
        status = "interpretation_missing"
    return {"status": status, "count_matches": proposed is not None and len(proposed) == observation["count"],
            "order_matches": proposed == observation["ids"], "claimed_ids": proposed}


PROJECT_TASK = "Model the open-title GET query: active=1 means open; zone stands for title. Sort title ASCII ignoring case ASC, then id ASC."
PROJECT_REQUIREMENTS = "Use table_check with your own filters and sort keys. Explain the hypothesis in plan. The tool result will be sent to Engineer."


def project_binding(parent):
    return {"parent": parent, "tool": "table_check", "dataset": DATASET, "dataset_sha256": DATASET_SHA256,
            "arguments": arguments_schema(), "max_calls": 1}
