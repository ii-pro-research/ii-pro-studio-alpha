from dataclasses import replace
import json
import time

import jwt
import pytest

from ii_pro_core.errors import Conflict, RecoveryRequired, ValidationError
from ii_pro_team.siwc import ISSUER, JWKS, TOKEN, RESOURCE, SCOPES
from ii_pro_team.text import DIAGNOSTIC_PROMPT, MODEL, FIRST_PROMPT, request_body
from test_gpt_text import MockHTTP, studio, prepare
from test_gpt_team import decision, wait
from test_siwc import flow, callback


pytestmark = pytest.mark.usefixtures("siwc_protocol_vault")

def test_operator_abandons_only_unknown_text_effect_without_rewriting_history(tmp_path):
    client = MockHTTP("lost")
    owner = studio(tmp_path, client)
    try:
        phase = prepare(owner)["phases"][0]
        owner.team.decide(decision(phase))
        assert wait(owner)["phases"][0]["status"] == "unknown"
        run = owner.school.core.runs.get(phase["run_id"])
        before = dict(
            owner.school.store.one("SELECT * FROM runs WHERE id=?", (run.id,))
        )
        events = [
            dict(r)
            for r in owner.school.store.all(
                "SELECT * FROM events WHERE run_id=?", (run.id,)
            )
        ]
        approval = owner.school.core.approvals.get(phase["approval"]["id"])
        assert owner.team.blocked()
        for options in ({}, {"accept_unknown_cost": True}, {"no_retry": True}):
            with pytest.raises(ValidationError):
                owner.team.abandon_unknown(
                    run.id, action_hash=run.action_hash, **options
                )
        with pytest.raises(RecoveryRequired):
            owner.team.abandon_unknown(
                run.id, action_hash="wrong", accept_unknown_cost=True, no_retry=True
            )
        args = dict(
            action_hash=run.action_hash, accept_unknown_cost=True, no_retry=True
        )
        owner.team.abandon_unknown(run.id, **args)
        owner.team.abandon_unknown(run.id, **args)
        assert not owner.team.blocked()
        assert owner.team.state()["abandoned_unknowns"] == [run.id]
        assert (
            dict(owner.school.store.one("SELECT * FROM runs WHERE id=?", (run.id,)))
            == before
        )
        assert [
            dict(r)
            for r in owner.school.store.all(
                "SELECT * FROM events WHERE run_id=?", (run.id,)
            )
        ] == events
        assert owner.school.core.approvals.get(phase["approval"]["id"]) == approval
        assert (
            len(
                owner.school.store.all(
                    "SELECT * FROM events WHERE kind='team.unknown_abandoned'"
                )
            )
            == 1
        )
        with pytest.raises(RecoveryRequired):
            owner.team.decide(decision(phase))
        with pytest.raises(RecoveryRequired):
            owner.school.core.runs.create(
                run.task_id, idempotency_key="forbidden-retry"
            )
        new = owner.team.prepare(
            {
                "request_id": "b" * 32,
                "project_id": "gpt-text",
                "text": DIAGNOSTIC_PROMPT,
            }
        )
        assert new["phases"][0]["run_id"] != run.id and new["phases"][0]["attempt"] == 1
        assert (
            new["phases"][0]["approval"]["status"] == "pending"
            and len(client.calls) == 1
        )
    finally:
        owner.close()


def test_completed_run_cannot_be_abandoned(tmp_path):
    owner = studio(tmp_path, MockHTTP())
    try:
        phase = prepare(owner)["phases"][0]
        owner.team.decide(decision(phase))
        wait(owner)
        run = owner.school.core.runs.get(phase["run_id"])
        with pytest.raises(RecoveryRequired):
            owner.team.abandon_unknown(
                run.id,
                action_hash=run.action_hash,
                accept_unknown_cost=True,
                no_retry=True,
            )
    finally:
        owner.close()


def test_acknowledgement_survives_restart_without_permitting_old_retry(tmp_path):
    owner = studio(tmp_path, MockHTTP("lost"))
    phase = prepare(owner)["phases"][0]
    owner.team.decide(decision(phase))
    wait(owner)
    run = owner.school.core.runs.get(phase["run_id"])
    owner.team.abandon_unknown(
        run.id, action_hash=run.action_hash, accept_unknown_cost=True, no_retry=True
    )
    owner.close()
    owner = studio(tmp_path, MockHTTP(), session="reopened")
    try:
        assert not owner.team.blocked()
        assert owner.school.core.runs.get(run.id).status == "unknown"
        with pytest.raises(RecoveryRequired):
            owner.team.decide(decision(phase))
    finally:
        owner.close()


def test_acknowledgement_never_exempts_running_or_nontext_effects(
    tmp_path, monkeypatch
):
    owner = studio(tmp_path, MockHTTP("lost"))
    try:
        phase = prepare(owner)["phases"][0]
        owner.team.decide(decision(phase))
        wait(owner)
        run = owner.school.core.runs.get(phase["run_id"])
        owner.team.abandon_unknown(
            run.id, action_hash=run.action_hash, accept_unknown_cost=True, no_retry=True
        )
        original = owner.school.store.all
        for changes in (
            {"status": "running"},
            {"executor": "worker_fixed"},
            {"effect_status": "partial"},
        ):
            row = {
                "id": run.id,
                "action_hash": run.action_hash,
                "executor": "team_text",
                "status": "unknown",
                "effect_status": "unknown",
                **changes,
            }

            def rows(sql, args=()):
                return (
                    [row]
                    if sql.startswith(
                        "SELECT id,action_hash,executor,status,effect_status"
                    )
                    else original(sql, args)
                )

            monkeypatch.setattr(owner.school.store, "all", rows)
            assert owner.team.blocked()
        monkeypatch.setattr(owner.school.store, "all", original)
    finally:
        owner.close()


def test_diagnostic_prompt_is_fixed_text_only_and_legacy_request_unchanged():
    body = request_body(DIAGNOSTIC_PROMPT)
    assert body["input"][1]["content"] == DIAGNOSTIC_PROMPT
    assert "single line" in body["input"][0]["content"]
    assert body["model"] == MODEL and body["reasoning"] == {"effort": "low"}
    assert body["tools"] == [] and body["store"] is False and body["stream"] is True
    from pathlib import Path

    archived = json.loads(
        (Path(__file__).parents[1] / "tests/fixtures/architect-request-body.json").read_text(
            encoding="utf-8"
        )
    )
    assert archived == request_body(FIRST_PROMPT)


def refresh_flow(tmp_path, fault=None):
    issuer, vault, pending = flow(tmp_path)
    prior = pending.complete(callback(pending, iss=ISSUER))
    vault.save(replace(prior, expires_at=time.time() - 10))
    calls = []

    def http(method, url, **kwargs):
        calls.append((method, url))
        if url == TOKEN:
            assert kwargs["form"] and kwargs["body"] == {
                "grant_type": "refresh_token",
                "client_id": prior.client_id,
                "refresh_token": prior.refresh_token,
                "resource": RESOURCE,
            }
            if fault == "network":
                raise Conflict("Synthetic refresh failure")
            claims = {
                "iss": ISSUER,
                "aud": prior.client_id,
                "sub": prior.subject,
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
                "nonce": pending.nonce,
            }
            if fault in {"iss", "aud", "sub", "nonce"}:
                claims[fault] = "wrong"
            if fault == "expired":
                claims["exp"] = int(time.time()) - 100
            result = {
                "access_token": "new-fixture-access",
                "refresh_token": "new-fixture-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": SCOPES,
                "id_token": jwt.encode(
                    claims, issuer.key, algorithm="RS256", headers={"kid": "fixture"}
                ),
            }
            if fault == "missing_id":
                del result["id_token"]
            if fault == "missing_scope":
                del result["scope"]
            if fault == "scope_loss":
                result["scope"] = "openid resource.invoke"
            if fault == "scope_escalation":
                result["scope"] = SCOPES + " extra_permission"
            if fault == "token_type":
                result["token_type"] = "other"
            if fault == "token_control":
                result["access_token"] = "secret\r\nheader"
            if fault == "lifetime":
                result["expires_in"] = True
            if fault == "signature":
                result["id_token"] = result["id_token"][:-8] + "invalid"
            return result
        assert url == JWKS
        return issuer.http(method, url, **kwargs)

    return vault, prior, calls, http


@pytest.mark.parametrize("fault", [None, "missing_id", "missing_scope"])
def test_explicit_refresh_rotates_only_own_credentials_and_preserves_registration(
    tmp_path, fault
):
    vault, prior, calls, http = refresh_flow(tmp_path, fault)
    updated = vault.refresh(http=http)
    assert (updated.client_id, updated.subject, updated.host_id) == (
        prior.client_id,
        prior.subject,
        prior.host_id,
    )
    assert updated.refresh_token == "new-fixture-refresh" and updated.ready(MODEL)
    assert (
        updated.reference != prior.reference
        and vault.load().reference == updated.reference
    )
    assert calls.count(("POST", TOKEN)) == 1
    vault.refresh(http=http)
    assert calls.count(("POST", TOKEN)) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "iss",
        "aud",
        "sub",
        "nonce",
        "expired",
        "scope_escalation",
        "token_type",
        "token_control",
        "lifetime",
        "signature",
        "network",
    ],
)
def test_invalid_refresh_never_overwrites_registration_or_retries(tmp_path, fault):
    vault, prior, calls, http = refresh_flow(tmp_path, fault)
    before = {f.name: f.read_bytes() for f in vault.root.glob("*.dpapi")}
    with pytest.raises(Conflict):
        vault.refresh(http=http)
    assert calls.count(("POST", TOKEN)) == 1
    assert {f.name: f.read_bytes() for f in vault.root.glob("*.dpapi")} == before


def test_scope_reduction_retains_rotated_credentials_but_blocks_generation(tmp_path):
    vault, prior, calls, http = refresh_flow(tmp_path, "scope_loss")
    with pytest.raises(Conflict):
        vault.refresh(http=http)
    saved = vault.load()
    assert saved.refresh_token == "new-fixture-refresh" and not saved.ready(MODEL)
