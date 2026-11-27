import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest

from ii_pro_core.errors import Conflict, ValidationError
from ii_pro_team.siwc import (
    Authorization,
    Vault,
    ISSUER,
    JWKS,
    TOKEN,
    RESOURCE,
    SCOPES,
    dpapi,
)


@pytest.mark.skipif(os.name == "nt", reason="Non-Windows fail-closed behavior")
def test_native_dpapi_fails_closed_on_non_windows():
    # The imported reference is the real implementation, outside the fixture.
    with pytest.raises(Conflict, match="requires Windows DPAPI"):
        dpapi(b"synthetic fixture")

pytestmark = pytest.mark.usefixtures("siwc_protocol_vault")

class Issuer:
    def __init__(self, fault=None):
        self.fault, self.calls, self.pending = fault, [], None
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def http(self, method, url, **kwargs):
        self.calls.append((method, url))
        if url == TOKEN:
            assert (
                kwargs["form"]
                and kwargs["body"]["redirect_uri"] == self.pending.redirect
            )
            assert kwargs["body"]["resource"] == RESOURCE
            assert kwargs["body"]["client_id"] == "oaiapp_fixture"
            assert kwargs["body"]["code_verifier"] == self.pending.verifier
            claims = {
                "iss": ISSUER,
                "aud": "oaiapp_fixture",
                "sub": "fixture-subject",
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
                "nonce": self.pending.nonce,
            }
            if self.fault in {"nonce", "aud", "iss", "sub"}:
                claims[self.fault] = "wrong"
            if self.fault == "expired":
                claims["exp"] = int(time.time()) - 60
            key = (
                self.key
                if self.fault != "signature"
                else rsa.generate_private_key(public_exponent=65537, key_size=2048)
            )
            return {
                "id_token": jwt.encode(
                    claims, key, algorithm="RS256", headers={"kid": "fixture"}
                ),
                "access_token": "private-fixture-access",
                "refresh_token": "private-fixture-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid resource.invoke" if self.fault == "scope" else SCOPES,
            }
        if url == JWKS:
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            jwk["kid"] = "fixture"
            return {"keys": [jwk]}
        if url == RESOURCE + "/models":
            return {
                "models": [
                    {"slug": "gpt-6.2-sol", "visibility": "list"},
                    {"slug": "hidden", "visibility": "hide"},
                ]
            }
        raise AssertionError("No other endpoint, especially inference, is allowed")


def flow(tmp_path, fault=None):
    issuer = Issuer(fault)
    vault = Vault(tmp_path / "auth")
    pending = Authorization(
        vault, "http://127.0.0.1:54321/auth/callback", http=issuer.http
    )
    issuer.pending = pending
    return issuer, vault, pending


def callback(p, **kwargs):
    return urlencode(
        {
            "state": p.state,
            "code": "one-time-fixture",
            "client_id": "oaiapp_fixture",
            **kwargs,
        }
    )


def test_public_dynamic_registration_pkce_exact_redirect_and_protected_store(tmp_path):
    issuer, vault, p = flow(tmp_path)
    params = parse_qs(urlsplit(p.url()).query)
    assert params["client_id"] == ["dynamic_agent_client"] and params[
        "agent_name_hint"
    ] == ["II PRO Studio"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["resource"] == [RESOURCE] and set(params["scope"][0].split()) == set(
        SCOPES.split()
    )
    assert p.verifier not in p.url() and "client_secret" not in params
    g = p.complete(callback(p))
    assert g.ready("gpt-6.2-sol")
    assert issuer.calls == [
        ("POST", TOKEN),
        ("GET", JWKS),
        ("GET", RESOURCE + "/models"),
    ]
    restored = vault.load()
    assert restored.reference == g.reference
    assert restored.access_token == "private-fixture-access" and restored.models == (
        "gpt-6.2-sol",
    )
    for file in vault.root.glob("*.dpapi"):
        assert (
            b"private-fixture" not in file.read_bytes()
            and b"access_token" not in file.read_bytes()
        )
    assert "private-fixture" not in repr(g)
    assert Vault(vault.root).host() == g.host_id
    p2 = Authorization(
        vault, "http://127.0.0.1:54322/auth/callback", selected=g, http=issuer.http
    )
    options = parse_qs(urlsplit(p2.url()).query)
    assert (
        options["client_id"] == ["oaiapp_fixture"] and "agent_name_hint" not in options
    )
    assert p2.host_id == g.host_id and p2.state != p.state and p2.nonce != p.nonce
    with pytest.raises(Conflict):
        p.complete(callback(p))
    assert len(issuer.calls) == 3


@pytest.mark.parametrize("fault", ["nonce", "aud", "iss", "expired", "signature"])
def test_untrusted_identity_and_missing_plan_scope_never_reach_model_list(
    tmp_path, fault
):
    issuer, vault, p = flow(tmp_path, fault)
    with pytest.raises(Conflict):
        p.complete(callback(p, iss=ISSUER, scope=SCOPES))
    assert not (vault.root / "active.dpapi").exists()
    assert all(url != RESOURCE + "/models" for _, url in issuer.calls)


@pytest.mark.parametrize(
    "kind",
    [
        "state",
        "missing_client",
        "dynamic_client",
        "duplicate",
        "denied",
        "expired_attempt",
    ],
)
def test_invalid_callback_never_exchanges_code(tmp_path, kind):
    issuer, vault, p = flow(tmp_path)
    query = callback(p)
    if kind == "state":
        query = callback(p, state="wrong")
    if kind == "missing_client":
        query = urlencode({"state": p.state, "code": "once"})
    if kind == "dynamic_client":
        query = callback(p, client_id="dynamic_agent_client")
    if kind == "duplicate":
        query += "&state=duplicate"
    if kind == "denied":
        query = urlencode({"state": p.state, "error": "access_denied"})
    if kind == "expired_attempt":
        p.created -= 301
    with pytest.raises((Conflict, ValidationError)):
        p.complete(query)
    assert issuer.calls == [] and vault.load() is None


@pytest.mark.parametrize(
    "uri",
    [
        "http://localhost:1234/auth/callback",
        "https://127.0.0.1:1234/auth/callback",
        "http://127.0.0.1:1234/callback",
        "http://127.0.0.1:1234/auth/callback?x=1",
    ],
)
def test_only_exact_loopback_callback_allowed(tmp_path, uri):
    with pytest.raises(ValidationError):
        Authorization(Vault(tmp_path), uri)


def test_valid_identity_without_plan_is_retained_but_never_used_for_inference(tmp_path):
    issuer, vault, p = flow(tmp_path, "scope")
    result = p.complete(callback(p, scope=SCOPES))
    assert (
        not result.ready("gpt-6.2-sol") and vault.load().client_id == "oaiapp_fixture"
    )
    assert issuer.calls == [("POST", TOKEN), ("GET", JWKS)]
    next_attempt = Authorization(
        vault, "http://127.0.0.1:54322/auth/callback", selected=result, http=issuer.http
    )
    assert parse_qs(urlsplit(next_attempt.url()).query)["prompt"] == ["consent"]


@pytest.mark.skipif(os.name != "nt", reason="Native Windows DPAPI coverage")
def test_dpapi_really_encrypts_only_our_fixture(tmp_path):
    raw = b"SYNTHETIC_CREDENTIAL_NOT_REAL"
    encrypted = dpapi(raw)
    assert raw not in encrypted and dpapi(encrypted, decrypt=True) == raw
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Native Windows file-lock coverage")
def test_only_one_login_can_rotate_this_hosts_credentials(tmp_path):
    vault = Vault(tmp_path / "auth")
    with vault.login_lock():
        with pytest.raises(Conflict):
            with Vault(vault.root).login_lock():
                raise AssertionError("Second login must not enter")
    with vault.login_lock():
        assert vault.host() == vault.host()


def test_catalog_failure_preserves_verified_registration_but_disables_inference(
    tmp_path,
):
    issuer, vault, p = flow(tmp_path)
    original = issuer.http

    def http(method, url, **kwargs):
        if url == RESOURCE + "/models":
            raise Conflict("Synthetic metadata outage")
        return original(method, url, **kwargs)

    p.http = http
    with pytest.raises(Conflict):
        p.complete(callback(p))
    saved = vault.load()
    assert saved.client_id == "oaiapp_fixture" and not saved.ready("gpt-6.2-sol")
    next_attempt = Authorization(
        vault, "http://127.0.0.1:54322/auth/callback", selected=saved, http=issuer.http
    )
    assert parse_qs(urlsplit(next_attempt.url()).query)["client_id"] == [
        "oaiapp_fixture"
    ]


@pytest.mark.parametrize("include_issuer", [True, False])
def test_callback_issuer_accepts_exact_official_value_or_legacy_absence(
    tmp_path, include_issuer
):
    issuer, vault, p = flow(tmp_path)
    query = callback(p, **({"iss": ISSUER} if include_issuer else {}))
    grant = p.complete(query)
    assert vault.load().reference == grant.reference
    assert issuer.calls.count(("POST", TOKEN)) == 1
    with pytest.raises(Conflict):
        p.complete(query)
    with pytest.raises(Conflict):
        p.complete(callback(p, iss=ISSUER, code="replaced-code"))
    assert issuer.calls.count(("POST", TOKEN)) == 1


@pytest.mark.parametrize(
    "value",
    [
        "",
        "https://auth.openai.com/",
        "https://auth.openai.com.evil.example",
        "https://evil.example/auth.openai.com",
        "https://auth.openai.com@evil.example",
        "http://auth.openai.com",
        "https://AUTH.OPENAI.COM",
        "https://auth.openai.com?x=1",
        "https://auth.openai.com#fragment",
        " https://auth.openai.com",
        "https://auth.openai.com\u200b",
        "https%3A%2F%2Fauth.openai.com",
    ],
)
def test_callback_issuer_mismatch_never_exchanges_or_saves_registration(
    tmp_path, value
):
    issuer, vault, p = flow(tmp_path)
    with pytest.raises(ValidationError, match="issuer"):
        p.complete(callback(p, iss=value))
    assert issuer.calls == [] and vault.load() is None
    # A rejected response with the correct state consumes this one-shot attempt.
    with pytest.raises(Conflict):
        p.complete(callback(p, iss=ISSUER))
    assert issuer.calls == []


@pytest.mark.parametrize("state", [None, "", "wrong"])
def test_official_issuer_does_not_replace_state_validation(tmp_path, state):
    issuer, vault, p = flow(tmp_path)
    values = parse_qs(callback(p, iss=ISSUER))
    if state is None:
        del values["state"]
    else:
        values["state"] = [state]
    with pytest.raises(Conflict):
        p.complete(urlencode(values, doseq=True))
    assert issuer.calls == [] and vault.load() is None


@pytest.mark.parametrize(
    "extra",
    [
        "iss=" + ISSUER,
        "iss=https://evil.example",
        "%69ss=" + ISSUER,
        "state=wrong",
        "code=another-code",
        "client_id=oaiapp_other",
        "scope=openid",
        "unexpected=value",
    ],
)
def test_issuer_callback_rejects_duplicate_or_unknown_query_parameters(tmp_path, extra):
    issuer, vault, p = flow(tmp_path)
    with pytest.raises(ValidationError, match="Invalid OAuth callback"):
        p.complete(callback(p, iss=ISSUER, scope=SCOPES) + "&" + extra)
    assert issuer.calls == [] and vault.load() is None


@pytest.mark.parametrize("iss", [ISSUER, "https://evil.example"])
def test_issuer_error_callback_never_exchanges_code(tmp_path, iss):
    issuer, vault, p = flow(tmp_path)
    with pytest.raises((Conflict, ValidationError)):
        p.complete(callback(p, iss=iss, error="access_denied"))
    assert issuer.calls == [] and vault.load() is None


@pytest.mark.parametrize("include_client", [True, False])
def test_verified_existing_client_reauthorizes_without_new_registration(
    tmp_path, include_client
):
    issuer, vault, p = flow(tmp_path)
    original = p.complete(callback(p, iss=ISSUER))
    saved = vault.load()
    p2 = Authorization(
        vault, "http://127.0.0.1:54322/auth/callback", selected=saved, http=issuer.http
    )
    options = parse_qs(urlsplit(p2.url()).query)
    assert options["client_id"] == [saved.client_id]
    assert "agent_name_hint" not in options
    assert p2.host_id == original.host_id
    assert p2.state != p.state and p2.nonce != p.nonce and p2.verifier != p.verifier
    issuer.pending, issuer.calls = p2, []
    values = parse_qs(callback(p2, iss=ISSUER))
    if not include_client:
        del values["client_id"]
    result = p2.complete(urlencode(values, doseq=True))
    assert result.client_id == saved.client_id and result.subject == saved.subject
    assert vault.load().reference == result.reference != original.reference
    assert issuer.calls.count(("POST", TOKEN)) == 1
    assert len(list(vault.root.glob("account-*.dpapi"))) == 1


@pytest.mark.parametrize("fault", ["client_id", "subject", "issuer"])
def test_existing_client_mismatch_never_replaces_verified_registration(tmp_path, fault):
    issuer, vault, p = flow(tmp_path)
    saved = p.complete(callback(p, iss=ISSUER))
    before = {f.name: f.read_bytes() for f in vault.root.glob("*.dpapi")}
    p2 = Authorization(
        vault, "http://127.0.0.1:54322/auth/callback", selected=saved, http=issuer.http
    )
    issuer.pending, issuer.calls = p2, []
    args = {"iss": ISSUER}
    if fault == "client_id":
        args["client_id"] = "oaiapp_other"
    elif fault == "issuer":
        args["iss"] = "https://evil.example"
    else:
        issuer.fault = "sub"
    with pytest.raises((Conflict, ValidationError)):
        p2.complete(callback(p2, **args))
    assert issuer.calls == (
        [("POST", TOKEN), ("GET", JWKS)] if fault == "subject" else []
    )
    assert {f.name: f.read_bytes() for f in vault.root.glob("*.dpapi")} == before


def test_concurrent_official_callbacks_exchange_code_only_once(tmp_path):
    issuer, vault, p = flow(tmp_path)
    query = callback(p, iss=ISSUER)

    def complete_once(_):
        try:
            return p.complete(query).client_id
        except Conflict:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(complete_once, range(2)))
    assert sorted(results) == ["oaiapp_fixture", "rejected"]
    assert issuer.calls.count(("POST", TOKEN)) == 1
    assert vault.load().client_id == "oaiapp_fixture"
