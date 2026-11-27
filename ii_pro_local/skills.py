"""Small, immutable skill catalog. Trust and evidence belong to existing Core."""

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import re

from ii_pro_core.errors import ValidationError
from ii_pro_core.security import digest, public_text

J_LOCAL = ("j-local-tool", "9f0b93661a9bdefcb4d2fc69f9f549afd5afbdb880f7ff1717112ce6360c5a4a")
J_WORKER = ("j-worker-receipt", "68682a6c10aebc78dfdf77274471f6c0c600a36dd96692e599789b61dd5c826c")
AB = ("i1-pilot-results", "02e838c7cd3e87f42a9e878711965f3bb00108662824e88457282bd07d35cb03")
G_LOCAL = ("g-local-tool", "c87d9d33bc3a568d398d882b8d198eb080c1a1bdf5fddafa788fe2b2955c6520")


@dataclass(frozen=True)
class Skill:
    id: str
    title: str
    applies_to: str
    inputs: tuple[str, ...]
    steps: tuple[str, ...]
    criterion: str
    limitation: str
    evidence: tuple[tuple[str, str], ...]
    guidance: str
    tools: tuple[str, ...] = ("finish",)
    version: int = 1
    history: tuple[str, ...] = ("v1 · выделен из наблюдений I–J",)

    @property
    def key(self):
        return f"skill-{self.id}-v{self.version}"

    @property
    def sha256(self):
        return digest(asdict(self))

    def reference(self):
        return {"id": self.id, "version": self.version, "sha256": self.sha256, "guidance": self.guidance}


CATALOG = (
    Skill("signature", "Сохранение сигнатуры", "правка функции", ("контракт", "кандидат"),
          ("Сопоставить имя и параметры.", "Проверить обязательные аргументы."),
          "Сигнатура совпадает с контрактом.", "Проверка структуры не оценивает поведение.", (AB,),
          "Compare the declared signature with the candidate before judging behavior."),
    Skill("logic", "Анализ логики", "ручная трассировка", ("код", "вход"),
          ("Проследить вычисление.", "Найти различающий пример."),
          "Объяснение связывает условие и результат.", "Прогноз требует отдельной проверки.", (G_LOCAL, AB),
          "Trace one concrete input through the relevant branch and explain the result."),
    Skill("compare", "Сравнение правки", "сопоставление фрагментов", ("исходник", "кандидат", "контракт"),
          ("Найти изменённое выражение.", "Применить один различающий вход к обеим версиям."),
          "Вывод указывает изменение и его эффект.", "Фрагмент не описывает всё приложение.", (G_LOCAL, J_LOCAL),
          "Trace the supplied input through both fragments and report the candidate result."),
    Skill("tool-choice", "Выбор инструмента", "исследование снимка или правки", ("цель", "объект", "допустимые инструменты"),
          ("Уточнить, какой объект проверяется.", "Выбрать действие, дающее нужное доказательство."),
          "Действие отвечает на вопрос о нужном объекте.", "Пустой Git diff не описывает отдельного кандидата.",
          (G_LOCAL, J_LOCAL), "Use the supplied candidate rather than Git diff for an unapplied patch."),
    Skill("conclusion", "Итоговое заключение", "отчёт по наблюдениям", ("вопрос", "наблюдения"),
          ("Ответить на вопрос.", "Назвать доказательства и неизвестное."),
          "Вывод содержит ответ и пределы проверки.", "Не выдумывать выполненных проверок.", (G_LOCAL, J_LOCAL),
          "State your conclusion and distinguish evidence from what still needs checking."),
    Skill("static-functional", "Граница статической проверки", "оценка доказательств", ("статический результат", "результат исполнения"),
          ("Назвать метод каждого наблюдения.", "Указать, какие выводы следуют из фактического запуска."),
          "Статический прогноз и проверенный результат различены.", "Разбор кода не доказывает функциональный успех.",
          (J_LOCAL, J_WORKER), "Label syntax evidence and executed test evidence separately."),
    Skill("test-failure", "Диагностика тестового провала", "разбор результатов исполнения", ("ожидание", "наблюдение", "кандидат"),
          ("Отделить отказ инфраструктуры.", "Сопоставить ожидание и фактический результат."),
          "Причина сформулирована как проверяемая гипотеза.", "Не менять критерии после теста.", (AB, J_WORKER),
          "Identify the first mismatch and distinguish test failure from infrastructure failure."),
)
BY_VERSION = {(skill.id, skill.version): skill for skill in CATALOG}
BY_ID = {skill.id: skill for skill in sorted(CATALOG, key=lambda skill: skill.version)}


def skill_is_verified(memory, skill):
    rows = memory.recall(skill.title, allowed_ids={skill.key}, language="python", tools=skill.tools)
    if rows:
        card = memory.get(skill.key)["body"]
        return (card["source"] == {"external": f"skill:{skill.key}:{skill.sha256}"}
                and rows[0]["observation"] == skill.guidance)
    return False


def verified_reference(memory, skill_id):
    versions = (skill for skill in CATALOG if skill.id == skill_id)
    return next((skill.reference() for skill in sorted(versions, key=lambda s: s.version, reverse=True)
                 if skill_is_verified(memory, skill)), None)


def packet(mode, source, candidate, provenance, *, skill=None):
    if mode not in {"comparison", "comparison_ast", "filter", "pilot", "evolution_a", "evolution_b"}:
        raise ValidationError("Unknown bounded review")
    public_text(source, limit=520, empty=False)
    public_text(candidate, limit=520, empty=mode == "filter")
    if (mode == "filter") != (candidate == "") or not re.fullmatch(r"[a-f0-9]{64}", provenance):
        raise ValidationError("Exact review inputs and their provenance are required")
    if skill is not None:
        if (type(skill) is not dict or skill.get("id") != "compare" or type(skill.get("version")) is not int
                or mode not in {"comparison", "comparison_ast", "pilot"}):
            raise ValidationError("Skill does not apply to this review")
        version = BY_VERSION.get(("compare", skill["version"]))
        if not version or skill != version.reference():
            raise ValidationError("Unknown skill version or identity")
    return {"mode": mode, "source": source, "candidate": candidate,
            "provenance": provenance, "skill": skill}


def changed_fragments(before, after):
    """Pick a bounded changed window from each actual version, never run code."""
    left, right = before.splitlines(), after.splitlines()
    for tag, i, j, a, b in SequenceMatcher(a=left, b=right, autojunk=False).get_opcodes():
        if tag != "equal":
            source = "\n".join(left[max(0, i - 1):max(i + 1, min(j + 1, i + 3))])
            candidate = "\n".join(right[max(0, a - 1):max(a + 1, min(b + 1, a + 3))])
            if source and candidate and source != candidate and max(len(source.encode()), len(candidate.encode())) <= 320:
                return source, candidate
    raise ValidationError("No bounded before/after fragment; specify a narrower review")
