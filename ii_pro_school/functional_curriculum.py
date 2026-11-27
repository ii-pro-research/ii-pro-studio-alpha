"""Developer-owned examples and disjoint private suites; not model outputs."""
from ii_pro_worker.school_contract import code_sha, tests_digest

CORRECT = "def sum_positive(values):\n    total = 0\n    for value in values:\n        if value > 0:\n            total += value\n    return total\n"
WRONG = "def sum_positive(values):\n    total = 0\n    for value in values:\n        if value > 0:\n            total = value\n    return total\n"
EXCEPTION = "def sum_positive(values):\n    return 1 // 0\n"
FIXTURES = {"correct": CORRECT, "overwrite_bug": WRONG, "ordinary_error": EXCEPTION}


def fixture_answer(fixture):
    return "```python\n" + FIXTURES[fixture] + "```\nDeveloper-authored readiness fixture, not a Qwen answer."


def developer_sources():
    return tuple(code_sha(code) for code in FIXTURES.values())


def suites():
    examples = {
        "baseline": [([], 0), ([2, -1, 3], 5), ([-2, -5], 0), ([7], 7), ([0, 4, 6], 10), ([1, 2, 3, 4], 10)],
        "practice": [([1, -9, 4], 5), ([9, 2], 11), ([-7, 0], 0), ([11, -11], 11), ([3, 3, 3], 9), ([0, 0, 8, 1], 9)],
        "control": [([8, 0, -4, 5], 13), ([13, -2, 17], 30), ([-8, -1, 0], 0), ([6, 6, -6, 6], 18), ([23], 23), ([5, 1, 7, -9, 2], 15)],
    }
    result = []
    for phase, cases in examples.items():
        tests = [{"id": phase[0] + str(index), "input": values, "expected": expected} for index, (values, expected) in enumerate(cases)]
        result.append({"id": "sum-positive-" + phase + "-v1", "phase": phase, "function": "sum_positive", "revision": 1,
                       "public": {"requirements": "Return the sum of strictly positive integers without changing the input list.",
                                  "signature": "def sum_positive(values):", "input_limit": 32,
                                  "restrictions": "One scalar function; bounded for/if or sum comprehension; no imports, I/O, attributes, recursion or while.",
                                  "public_examples": ["[4, -3, 2] -> 6"]},
                       "cases": tests, "tests_digest": tests_digest(tests)})
    return result
