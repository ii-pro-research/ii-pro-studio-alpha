"""Frozen initial fixtures. Private exams are stored, never sent by v0.1."""


def initial_lessons() -> list[dict]:
    common = {
        "revision": 1, "kind": "practice", "max_attempts": 3,
        "restrictions": ["Return exactly one Python function in a python code fence.",
                         "Do not add imports, I/O, other functions, or execute any code.",
                         "Keep the function name and parameters. Explain briefly after the code."],
        "rubric": {"syntax": 10, "signature": 10, "single_function_scope": 10,
                   "static_evidence": 15, "functional_pending": 55},
    }
    practice = [
        {"id": "bug-positive-sum", "title": "Find and fix the accumulator bug",
         "function": "sum_positive", "arguments": ["values"],
         "source": "def sum_positive(values):\n    total = 0\n    for value in values:\n        if value > 0:\n            total = value\n    return total\n",
         "requirements": "For a list of integers, return the sum of strictly positive elements. Empty input returns 0. Explain the bug and repair it without changing the signature.",
         "public_examples": ["[2, -1, 3] -> 5", "[] -> 0", "[-2, 0] -> 0"],
         "reference_criteria": ["Accumulate all positive values instead of overwriting the accumulator.", "Do not count zero or negative values.", "Do not mutate the input."]},
        {"id": "write-clamp", "title": "Write an integer clamp",
         "function": "clamp_int", "arguments": ["value", "lower", "upper"],
         "source": "def clamp_int(value, lower, upper):\n    pass\n",
         "requirements": "Accept exact ints only (bool is not an int here); otherwise raise TypeError. Raise ValueError when lower > upper. Otherwise clamp value to inclusive bounds without I/O.",
         "public_examples": ["(5, 1, 9) -> 5", "(-2, 1, 9) -> 1", "(12, 1, 9) -> 9"],
         "reference_criteria": ["Exact-type checks include all arguments.", "Equal bounds are valid.", "Check invalid range."]},
        {"id": "improve-unique", "title": "Improve stable deduplication",
         "function": "unique_strings", "arguments": ["values"],
         "source": "def unique_strings(values):\n    result = []\n    for value in values:\n        if value not in result:\n            result.append(value)\n    return result\n",
         "requirements": "Input is a list of strings. Preserve first-occurrence order, do not mutate input, and improve average complexity from quadratic to linear with a set. Explain the trade-off in memory.",
         "public_examples": ["['a', 'b', 'a'] -> ['a', 'b']", "[] -> []"],
         "reference_criteria": ["Use a membership set plus ordered output.", "Case-sensitive string identity.", "Describe extra memory cost."]},
    ]
    result = [{**common, **lesson} for lesson in practice]
    # Exam conditions are intentionally absent from training prompts/exports.
    for lesson_id, fn, args, requirements in (
        ("exam-even-sum", "sum_even", ["values"], "Sum every even integer, including negative values, without mutating input; empty input is zero."),
        ("exam-range-count", "count_in_range", ["values", "lower", "upper"], "Count integers inside inclusive bounds; invalid bounds raise ValueError; do not mutate input."),
        ("exam-casefold-unique", "unique_casefold", ["values"], "Deduplicate strings by casefold, preserving the spelling and order of the first occurrence; input unchanged."),
    ):
        result.append({**common, "id": lesson_id, "kind": "exam", "max_attempts": 1,
                       "title": "Independent control exercise", "function": fn, "arguments": args,
                       "source": "", "requirements": requirements, "public_examples": [],
                       "reference_criteria": ["Independent oracle required; no result is recorded until actually assessed."]})
    return result


def training_prompt(lesson: dict) -> str:
    if lesson["kind"] != "practice":
        raise ValueError("Private examinations are not released by the training path")
    return "\n\n".join([
        "Python practice. " + lesson["title"], lesson["requirements"],
        "Restrictions:\n" + "\n".join(lesson["restrictions"]),
        "Public examples:\n" + "\n".join(lesson["public_examples"]),
        "Source:\n```python\n" + lesson["source"] + "```",
    ])
