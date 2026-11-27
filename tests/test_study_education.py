import json
from threading import Thread

import pytest

from ii_pro_core.errors import Conflict, RecoveryRequired, ValidationError
from ii_pro_core.security import Action, digest
from ii_pro_school.study_curriculum import CURRICULUM_SHA256, HINT, comparative_suites
from ii_pro_worker.school_contract import validate_code
from ii_pro_ui.server import LocalServer
from ii_pro_ui.training import Training
from test_engineering_memory import entry
from test_devkit_bridge import MockBridge, require_devkit
from test_gpt_engineering import draft
from test_gpt_text import grant
from ii_pro_team.devkit import DevkitTextAdapter
from test_local_engine_ui import decision, mock_model, registry, request, settled  # noqa: F401,F811
from test_school_v04_ui import factory as ollama_factory
from test_school_v04_ui import ollama  # noqa: F401,F811
from test_school_v05_ui import open_training, worker_factory


def test_frozen_starters_fit_worker_contract():
    suites = comparative_suites()
    assert len({s['id'] for s in suites}) == 12
    assert sum(len(s['cases']) for s in suites) == 36
    assert digest(suites) == CURRICULUM_SHA256
    for suite in suites:
        validate_code(suite['public']['source'])
        assert not {'cases', 'expected', 'reference'} & suite['public'].keys()


def test_experiment_arms_freeze_original_context_and_reject_revocation(tmp_path):
    training = open_training(tmp_path)
    training._open()
    try:
        edu = training.education
        with pytest.raises(Conflict):
            edu.create_plan({'id': 'a' * 32})
        ident = 'pricing-h1-discount-evidence'
        card = {**entry(), 'tags': ['python', 'debugging'],
                'observation': 'Python: valid syntax does not demonstrate behavior.'}
        edu.core.memory.record(ident, card, tested=True)
        edu.core.memory.assess(ident, 'verified', reviewer='Fixture operator', reason='Fixture only')
        edu.create_plan({'id': 'a' * 32})
        plan = edu.plan()
        assert plan['options']['num_ctx'] == 2048 and plan['attempts_per_task'] == 1
        ids = set()
        suite = next(iter(edu.suites))
        for arm, cycle in plan['arms'].items():
            edu.exam({'arm': arm, 'suite': suite})
            row = edu.store.one('SELECT run_id FROM school_v2_proposals WHERE cycle_id=?', (cycle,))
            run = edu.core.runs.get(row[0])
            ids.add(run.id)
            action = json.loads(run.action_json)['parameters']
            prompt = action['prompt']
            assert len(prompt.encode()) <= 1450
            assert 'check_1' not in prompt and 'expected' not in prompt
            assert (card['observation'] in prompt) == (arm == 'b')
            assert (HINT in prompt) == (arm == 'c')
            edu.exam({'arm': arm, 'suite': suite})
            assert len(edu.store.all('SELECT run_id FROM school_v2_proposals WHERE cycle_id=?', (cycle,))) == 1
        assert len(ids) == 3
        assert all(v['total'] == 0 for v in edu.state()['arms'].values())
        edu.core.memory.assess(ident, 'revoked', reviewer='Fixture operator', reason='Fixture revocation')
        with pytest.raises(Conflict):
            edu.exam({'arm': 'b', 'suite': list(edu.suites)[1]})
        assert len(edu.store.all('SELECT * FROM school_responses')) == 0
    finally:
        training.close()


def test_comparative_suite_is_not_available_as_plain_practice(tmp_path):
    training = open_training(tmp_path)
    training._open()
    try:
        edu = training.education
        cycle = edu.domain.create_cycle('ui-qwen')
        with pytest.raises(ValidationError):
            edu.domain.prepare_model_request(cycle, next(iter(edu.suites)))
    finally:
        training.close()


def test_pilot_scope_accepts_only_abandoned_text_and_keeps_run_history(tmp_path, registry, ollama, monkeypatch):  # noqa: F811
    require_devkit()
    projects, _ = registry
    training = Training(tmp_path / "pilot", "session", include_ollama=True, include_worker=True, include_team=True,
                        local_projects=projects, worker_factory=worker_factory, real_adapter_factory=ollama_factory(ollama),
                        text_adapter_factory=lambda: DevkitTextAdapter(grant(), bridge=MockBridge("eof"), mock=True))
    training._open()
    try:
        edu = training.education
        team = training.team
        core = edu.core
        draft_view = team.prepare(draft("implement"))["phases"][0]
        permit = draft_view["approval"]
        core.approvals.decide(permit["id"], allow=True)
        old = core.runs.execute(draft_view["run_id"], approval_id=permit["id"])
        assert old.status == "unknown"
        events = core.events(old.task_id)
        used = core.approvals.get(permit["id"])

        card = {**entry(), "tags": ["python"]}
        core.memory.record("pricing-h1-discount-evidence", card, tested=True)
        core.memory.assess("pricing-h1-discount-evidence", "verified", reviewer="Fixture", reason="Fixture")
        with pytest.raises(Conflict):
            edu.prepare_pilot({})
        monkeypatch.setattr("ii_pro_ui.education.PILOT_CARD_SHA256", digest(card))
        edu.prepare_pilot({})
        edu.prepare_pilot({})
        assert edu.store.one("SELECT count(*) FROM school_v2_proposals")[0] == 4
        plan = edu.plan()
        first, second, other = list(edu.suites)[:3]
        runs = {}
        for arm, suite in (("a", first), ("b", first), ("b", second), ("a", second), ("c", first), ("a", other)):
            edu.exam({"arm": arm, "suite": suite})
            runs[arm, suite] = edu.store.one("SELECT run_id FROM school_v2_proposals WHERE cycle_id=? AND suite_id=?",
                                           (plan["arms"][arm], suite))[0]
        assert all(training.functional.blocked(run) for run in runs.values())
        team.abandon_unknown(old.id, action_hash=old.action_hash, accept_unknown_cost=True, no_retry=True)
        assert training.functional.blocked()
        for (arm, suite), run in runs.items():
            assert training.functional.blocked(run) == (arm == "c" or suite == other)
        assert core.runs.get(old.id) == old and core.events(old.task_id) == events
        assert core.approvals.get(permit["id"]) == used
        edu.require_pilot_order(runs["a", first])
        with pytest.raises(RecoveryRequired):
            edu.require_pilot_order(runs["b", first])
        assert all(core.runs.get(run).status == "created" for run in runs.values())
        first_run = runs["a", first]
        permit = training._attempt(first_run)["approval"]
        approval = {"run_id": first_run, "approval_id": permit["id"], "spec_digest": permit["spec_digest"], "allow": True}
        with pytest.raises(Conflict):
            training.decide(approval)
        training._authorized_real_run = first_run
        training.decide(approval)
        for thread in tuple(training.threads.values()):
            thread.join(4)
        assert core.runs.get(first_run).status == "succeeded"
        assert len([path for path, _ in ollama.calls if path == "/api/chat"]) == 1
        edu.require_pilot_order(runs["b", first])
        edu.functional({"arm": "a", "suite": first})
        worker_run = edu.state()["arms"]["a"]["attempts"][first]["functional_run"]
        assert edu.pilot_run(worker_run) and not training.functional.blocked(worker_run)
        permit = training.functional.view(worker_run)["approval"]
        with pytest.raises(Conflict, match="Separate user permission"):
            training.functional.decide({"run_id": worker_run, "approval_id": permit["id"], "spec_digest": permit["spec_digest"], "allow": True})
        assert core.runs.get(worker_run).status == "created"
        # Another unresolved operation is not pardoned by an exact cloud abandonment.
        second_view = team.prepare({**draft("implement"), "request_id": "f" * 32})["phases"][0]
        newer = second_view["approval"]
        core.approvals.decide(newer["id"], allow=True)
        assert core.runs.execute(second_view["run_id"], approval_id=newer["id"]).status == "unknown"
        assert training.functional.blocked(runs["a", first])
    finally:
        training.close()


def test_structured_card_requires_consumed_approval_before_recall(tmp_path, monkeypatch):
    training = open_training(tmp_path)
    training._open()
    try:
        edu = training.education
        core = edu.core
        card = {**entry(), 'category': 'debugging', 'language': 'python', 'tools': ['finish'],
                'task': 'Python regression review', 'mistake': 'Reasoned from syntax only', 'approach': 'Use a functional check'}
        core.memory.record('card', card, tested=True)
        with pytest.raises(Conflict):
            core.memory.assess('card', 'verified', reviewer='Operator', reason='Approval missing')
        assert core.memory.recall('Python regression') == []
        # Unit fixture replaces provenance verification here; the real H.1 receipt
        # is checked independently without changing its actual trust status.
        monkeypatch.setattr(edu.verifier, 'evidence', lambda entry: None)
        task = core.tasks.create('Review fixed evidence fixture', executor=edu.verifier.name, idempotency_key='verify-card')
        core.tasks.transition(task.id, 'planned')
        run = core.runs.create(task.id, idempotency_key='verify-card', action=Action.build('memory.verify', {
            'entry': 'card', 'sha256': core.memory.get('card')['sha256'],
        }))
        approval = core.approvals.request(run.id)
        assert core.memory.recall('Python regression') == []
        assert edu.decide_knowledge({'run': run.id, 'approval': approval.id, 'digest': approval.spec_digest})['status'] == 'succeeded'
        assert core.approvals.get(approval.id).status == 'consumed'
        assert [item['id'] for item in core.memory.recall('Python regression')] == ['card']
        with pytest.raises(Conflict):
            edu.decide_knowledge({'run': run.id, 'approval': approval.id, 'digest': approval.spec_digest})
    finally:
        training.close()


def test_http_lesson_proposes_only_structured_finish_and_preserves_mentor_provenance(tmp_path, registry, monkeypatch):  # noqa: F811
    projects, _ = registry
    calls = mock_model(monkeypatch, [{"plan": "Compare supplied expressions", "tool": "finish", "args": {
        "report": "Fixture conclusion", "defect": "Sign", "comparison": "Plus and minus",
        "prediction": "90", "distinguishing_test": "Trace a nonzero discount", "evidence": [],
    }}])
    server = LocalServer(0, None)
    server.training = Training(tmp_path / "study", server.token, include_ollama=True, include_worker=True,
                               local_projects=projects, local_live=True, worker_factory=worker_factory)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.03})
    thread.start()
    base, job = "/api/school/education", "b" * 32
    try:
        payload = {"job": job, "mode": "without_memory"}
        assert request(server, base + "/lesson", payload, trusted=False)[0] == 403
        code, detail = request(server, base + "/lesson", payload)
        assert code == 200, detail
        assert not calls
        operation = decision(detail)
        assert request(server, "/api/local-engine/decision", operation)[0] == 200
        result = settled(server, job)
        assert result["status"] == "finished"
        assert result["results"]["1"]["result"]["functional_tests"] == "not_run"
        assert result["results"]["1"]["result"]["prediction"] == "90"
        branches = calls[0]["format"]["oneOf"]
        assert len(calls) == len(branches) == 1
        assert branches[0]["properties"]["tool"]["enum"] == ["finish"]
        assert "verified_reference_data" not in calls[0]["messages"][1]["content"]
        assert request(server, base + "/feedback", {"job": job, "author": "Operator", "text": "Trace the two expressions.",
                                                   "next_step": "Explain the zero percent case."})[0] == 200
        code, followup = request(server, base + "/lesson", {"job": "c" * 32, "mode": "mentored", "after": job})
        assert code == 200 and followup['status'] == 'awaiting_approval' and len(calls) == 1
        assert request(server, base)[1]['lessons'][1]['feedback'][0]['origin'] == 'manual'
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()
