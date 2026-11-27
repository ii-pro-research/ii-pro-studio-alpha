"""Frozen, developer-owned comparative curriculum. Private evaluation cases stay server-side."""

from ii_pro_core.security import digest
from ii_pro_worker.school_contract import tests_digest

MODES = {"a": "without_memory", "b": "verified_memory", "c": "mentored"}
CURRICULUM_SHA256 = "d258c3eed6472f48ad12e7c2a5b1b43394945ff233f09ccd08099a8f91e6587d"
HINT = "Trace a boundary case by hand, then explain which test would distinguish the original from the correction."
LESSON = {
    "id": "logic-or-syntax",
    "title": "Логическая ошибка или ошибка синтаксиса",
    "task": "Explain the logical defect in pricing.py, compare the two expressions and predict discounted(100, 10).",
    "requirements": "Explain which functional test could confirm your prediction. Separate observations and claims.",
    "facts": ["Исправленный кандидат прошёл 6/6.", "Отрицательный кандидат прошёл 2/6; Workbench выбрал исходную версию."],
    "observed_errors": ["git_diff дал пустой результат.", "analyze_python подтвердил синтаксис исходника. Вывода о логике не было."],
    "mentor": ["Сравнить изменённое арифметическое выражение.", "Сформулировать пример, отличающий исходную версию от правки."],
    "assumptions": ["Синтаксическая корректность сама по себе ничего не говорит о результате.", "Результат на новых задачах ещё предстоит измерить."],
}

# The bounded Worker uses its historical entrypoint for all integer-list exercises.
EXAMS = (
    ("sum-offset", "Арифметика", "Sum every integer. The starter starts from the wrong offset.",
     "    total = 1\n    for x in values:\n        total += x\n    return total\n", [([], 0), ([2, -1, 4], 5), ([-2, -4], -6)]),
    ("count-zero", "Условия", "Count entries equal to zero, including consecutive zeros.",
     "    return sum(1 for x in values if x != 0)\n", [([0, 3, 0], 2), ([3, -1], 0), ([0, 0, 0], 3)]),
    ("count-nonnegative", "Границы", "Count all nonnegative entries. Zero is nonnegative.",
     "    return sum(1 for x in values if x > 0)\n", [([0, -2, 4], 2), ([], 0), ([0, -3, 0], 2)]),
    ("even-sum", "Условия", "Sum even integers, including negative even integers.",
     "    return sum(x for x in values if x % 2 != 0)\n", [([1, 2, 3, 6], 8), ([-4, 0, 3], -4), ([1, 3], 0)]),
    ("negative-magnitude", "Арифметика", "Sum the absolute values of strictly negative entries only.",
     "    return sum(x for x in values if x < 0)\n", [([-3, 4, -5], 8), ([1, 0], 0), ([-7], 7)]),
    ("alternating-sum", "Циклы", "Return x0 - x1 + x2 - x3 and so on. Empty lists return zero.",
     "    total = 0\n    for x in values:\n        total += x\n    return total\n", [([4, 1, 2], 5), ([1, 7], -6), ([], 0)]),
    ("largest-or-zero", "Границы", "Return the largest entry, or zero for an empty list. Negative maxima are valid.",
     "    return max(x for x in values)\n", [([], 0), ([-3, -7], -3), ([2, 6, 1], 6)]),
    ("sum-valid-scores", "Входные данные", "Sum scores from 0 through 100 inclusive. Ignore out-of-range integers.",
     "    return sum(x for x in values if x >= 0)\n", [([100, 101, 7, -2], 107), ([-1, 130], 0), ([0, 70], 70)]),
    ("count-two-digit", "Границы", "Count positive two-digit integers, from 10 through 99 inclusive.",
     "    return sum(1 for x in values if 10 < x < 99)\n", [([9, 10, 99, 100], 2), ([-47, 47, 0], 1), ([], 0)]),
    ("sum-absolute", "Входные данные", "Sum the magnitudes of all integers, including negative entries.",
     "    return sum(x for x in values)\n", [([-2, 5, -1], 8), ([], 0), ([0, -9], 9)]),
    ("negative-balance", "Проверка правки", "Return 1 when the sum of the whole list is negative; otherwise return 0. Name a check for cancellation.",
     "    return 1 if sum(x for x in values if x < 0) < 0 else 0\n", [([-5, 5], 0), ([2, -5], 1), ([], 0)]),
    ("count-decreases", "Выбор проверки", "Count strict decreases between adjacent entries. Explain a distinguishing test with ties.",
     "    count = 0\n    previous = 0\n    for x in values:\n        if x <= previous:\n            count += 1\n        previous = x\n    return count\n", [([5, 5, 2], 1), ([-2, -5, -1], 1), ([0], 0)]),
)


def comparative_suites():
    result = []
    for name, category, requirements, source, examples in EXAMS:
        cases = [{"id": "check_" + str(i), "input": values, "expected": answer}
                 for i, (values, answer) in enumerate(examples, 1)]
        public = {"title": name, "category": category, "requirements": requirements,
                  "signature": "def sum_positive(values):", "input_limit": 32,
                  "restrictions": "Keep the historical Worker entrypoint name. Use one bounded Python function; no imports or I/O.",
                  "public_examples": [], "source": "def sum_positive(values):\n" + source}
        result.append({"id": "study-" + name + "-v1", "phase": "control", "track": "comparative-v1",
                       "function": "sum_positive", "revision": 1, "public": public,
                       "task_digest": digest(public), "cases": cases, "tests_digest": tests_digest(cases)})
    if digest(result) != CURRICULUM_SHA256:
        raise ValueError("Comparative curriculum differs from its frozen revision")
    return result
