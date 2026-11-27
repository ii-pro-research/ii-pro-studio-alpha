"""Public-client SIWC for this app only; never imports another client's login.

Live entry is explicit operator CLI. No import, UI poll, or Core prepare starts
OAuth. Credentials are DPAPI-protected, never part of the Core action/spec.
"""

import base64
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, field, replace
import hashlib
import hmac
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit, parse_qs
import uuid

from ii_pro_core.errors import Conflict, ValidationError
from ii_pro_core.security import no_links, canonical
from ii_pro_school.adapters import _strict_json

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
JWKS = ISSUER + "/.well-known/jwks.json"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
METADATA_MAX_BYTES = 128 * 1024
CATALOG_MAX_BYTES = 2 * 1024 * 1024


def request(method, url, *, token=None, body=None, form=False, timeout=20):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"auth.openai.com", "api.openai.com"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError("Only fixed OpenAI HTTPS endpoints are allowed")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = (
            "application/x-www-form-urlencoded" if form else "application/json"
        )
        body = (urlencode(body) if form else json.dumps(body)).encode()
    conn = HTTPSConnection(parsed.netloc, timeout=timeout)
    try:
        conn.request(method, parsed.path, body=body, headers=headers)
        response = conn.getresponse()
        # SIWC catalogs include large model metadata (850,906 bytes observed).
        # Keep the token/JWKS limit unchanged; only this exact GET has more room.
        limit = (
            CATALOG_MAX_BYTES
            if method == "GET" and url == RESOURCE + "/models"
            else METADATA_MAX_BYTES
        )
        data = response.read(limit + 1)
        if response.status != 200 or len(data) > limit:
            # Do not include URLs, bodies, tokens, or provider exception text.
            raise Conflict("SIWC metadata/token request rejected")
        return _strict_json(data)
    finally:
        conn.close()


def dpapi(data, *, decrypt=False):
    if os.name != "nt":
        raise Conflict("This credential store requires Windows DPAPI")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    if decrypt:
        function = library.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(Blob),
        ]
        args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        function = library.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(Blob),
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(Blob),
        ]
        args = (
            ctypes.byref(source),
            "II PRO Studio SIWC",
            None,
            None,
            None,
            1,
            ctypes.byref(target),
        )
    function.restype = wintypes.BOOL
    if not function(*args):
        raise Conflict("Windows credential protection failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(target.data)


@dataclass(frozen=True, repr=False)
class Grant:
    client_id: str
    subject: str
    host_id: str
    grant_id: str
    expires_at: float
    scopes: tuple[str, ...]
    models: tuple[str, ...]
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    id_token: str = field(repr=False)

    @property
    def reference(self):
        return hashlib.sha256(
            canonical(
                [self.client_id, self.subject, self.host_id, self.grant_id, self.scopes]
            ).encode()
        ).hexdigest()

    def ready(self, model):
        return (
            self.expires_at > time.time() + 65
            and PLAN_SCOPE in self.scopes
            and "resource.invoke" in self.scopes
            and model in self.models
        )


class Vault:
    def __init__(self, root):
        self.root = Path(root)

    @contextmanager
    def login_lock(self):
        import msvcrt

        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "login.lock"
        no_links(path)
        with path.open("a+b") as stream:
            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise Conflict("Another Studio sign-in is active") from None
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    def _write(self, name, data):
        self.root.mkdir(parents=True, exist_ok=True)
        no_links(self.root)
        path = self.root / name
        no_links(path)
        temp = self.root / ("pending-" + uuid.uuid4().hex)
        try:
            with temp.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()

    def host(self):
        path = self.root / "host.json"
        no_links(path)
        if path.exists():
            value = _strict_json(path.read_bytes())["host_id"]
            if not value.startswith("urn:uuid:") or uuid.UUID(value[9:]).version != 4:
                raise ValidationError("Invalid stable host identity")
            return value
        value = "urn:uuid:" + str(uuid.uuid4())
        self._write("host.json", canonical({"host_id": value}).encode())
        return value

    def load(self):
        path = self.root / "active.dpapi"
        no_links(path)
        if not path.exists():
            return None
        raw = path.read_bytes()
        if len(raw) > 131072:
            raise ValidationError("Credential record quota exceeded")
        data = _strict_json(dpapi(raw, decrypt=True))
        for key in ("scopes", "models"):
            data[key] = tuple(data[key])
        return Grant(**data)

    def save(self, grant):
        # Each client/account has its own encrypted record. No file is a model input.
        encrypted = dpapi(canonical(grant.__dict__).encode())
        account = hashlib.sha256(
            (grant.client_id + ":" + grant.subject).encode()
        ).hexdigest()
        self._write("account-" + account + ".dpapi", encrypted)
        self._write("active.dpapi", encrypted)

    def refresh(self, *, http=request):
        """One explicit refresh, serialized with sign-in; no registration/retry."""
        with self.login_lock():
            selected = self.load()
            if selected is None or not selected.refresh_token:
                raise Conflict("Saved Studio refresh authorization required")
            if selected.expires_at > time.time() + 65:
                return selected
            tokens = http(
                "POST",
                TOKEN,
                body={
                    "grant_type": "refresh_token",
                    "client_id": selected.client_id,
                    "refresh_token": selected.refresh_token,
                    "resource": RESOURCE,
                },
                form=True,
            )
            received = time.time()
            try:
                lifetime = tokens["expires_in"]
                if type(lifetime) is not int or not 65 < lifetime <= 3600:
                    raise ValueError()
                if tokens.get("token_type", "").lower() != "bearer":
                    raise ValueError()
                for key in ("access_token", "refresh_token"):
                    value = tokens[key]
                    if (
                        not isinstance(value, str)
                        or not 1 <= len(value) <= 32000
                        or any(not 33 <= ord(c) <= 126 for c in value)
                    ):
                        raise ValueError()
                scope = tokens.get("scope", " ".join(selected.scopes))
                if not isinstance(scope, str) or len(scope) > 4096:
                    raise ValueError()
                scopes = tuple(sorted(set(scope.split())))
                if not set(scopes) <= set(selected.scopes):
                    raise ValueError()
                identity = tokens.get("id_token", selected.id_token)
                if "id_token" in tokens:
                    import jwt

                    if not isinstance(identity, str) or not 1 <= len(identity) <= 32000:
                        raise ValueError()
                    header = jwt.get_unverified_header(identity)
                    if header.get("alg") != "RS256":
                        raise ValueError()
                    keys = [
                        k
                        for k in http("GET", JWKS)["keys"]
                        if k.get("kid") == header.get("kid")
                        and k.get("kty") == "RSA"
                        and k.get("use", "sig") == "sig"
                    ]
                    if len(keys) != 1:
                        raise ValueError()
                    claims = jwt.decode(
                        identity,
                        jwt.PyJWK.from_dict(keys[0], algorithm="RS256").key,
                        algorithms=["RS256"],
                        issuer=ISSUER,
                        audience=selected.client_id,
                        leeway=5,
                        options={"require": ["sub", "exp", "iat"]},
                    )
                    if claims["sub"] != selected.subject:
                        raise ValueError()
                    # The old token was validated before DPAPI storage. It is
                    # only a source of original nonce/auth_time for comparison.
                    prior = jwt.decode(
                        selected.id_token, options={"verify_signature": False}
                    )
                    if "nonce" in claims and claims["nonce"] != prior.get("nonce"):
                        raise ValueError()
                    if "auth_time" in claims and claims["auth_time"] != prior.get(
                        "auth_time"
                    ):
                        raise ValueError()
            except Exception:
                raise Conflict(
                    "Studio refresh response validation failed; no retry"
                ) from None
            permitted = {"openid", PLAN_SCOPE, "resource.invoke"} <= set(scopes)
            grant = replace(
                selected,
                grant_id=uuid.uuid4().hex,
                expires_at=received + lifetime,
                scopes=scopes,
                models=selected.models if permitted else (),
                access_token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
                id_token=identity,
            )
            self.save(grant)  # Preserve rotated credentials even on scope reduction.
            if not permitted:
                raise Conflict("Refreshed authorization lacks required plan scopes")
            return grant


class Authorization:
    """One pending OAuth transaction; never makes an inference request."""

    def __init__(self, vault, redirect_uri, *, selected=None, http=request):
        parts = urlsplit(redirect_uri)
        if (
            parts.scheme != "http"
            or parts.hostname != "127.0.0.1"
            or parts.path != "/auth/callback"
            or not parts.port
            or parts.query
            or parts.fragment
            or parts.username
        ):
            raise ValidationError("Exact loopback callback required")
        self.vault, self.redirect, self.selected, self.http = (
            vault,
            redirect_uri,
            selected,
            http,
        )
        self.state, self.nonce, self.verifier = (
            secrets.token_urlsafe(32) for _ in range(3)
        )
        self.created, self.used = time.monotonic(), False
        self.lock = threading.Lock()
        self.host_id = vault.host()

    def url(self):
        client_id = self.selected.client_id if self.selected else "dynamic_agent_client"
        params = {
            "client_id": client_id,
            "ext_agent_host_id": self.host_id,
            "response_type": "code",
            "redirect_uri": self.redirect,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": self.state,
            "nonce": self.nonce,
            "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(
                hashlib.sha256(self.verifier.encode()).digest()
            )
            .decode()
            .rstrip("="),
        }
        if not self.selected:
            params["agent_name_hint"] = "II PRO Studio"
        elif not {PLAN_SCOPE, "resource.invoke"} <= set(self.selected.scopes):
            params["prompt"] = "consent"
        return AUTHORIZE + "?" + urlencode(params)

    def complete(self, query):
        with self.lock:
            values = parse_qs(query, keep_blank_values=True, max_num_fields=10)
            if any(len(v) != 1 for v in values.values()) or set(values) - {
                "state",
                "code",
                "client_id",
                "scope",
                "iss",
                "error",
                "error_description",
            }:
                raise ValidationError("Invalid OAuth callback")
            state = values.get("state", [""])[0]
            if (
                not hmac.compare_digest(state, self.state)
                or self.used
                or time.monotonic() - self.created > 300
            ):
                raise Conflict("OAuth attempt missing, consumed or expired")
            self.used = True
            # RFC 9207: compare the decoded issuer exactly, before any code exchange.
            # Missing iss remains compatible with our single, fixed issuer flow.
            if "iss" in values and values["iss"][0] != ISSUER:
                raise ValidationError("Unexpected OAuth callback issuer")
            if "error" in values:
                raise Conflict("OAuth consent not granted")
            client_id = values.get(
                "client_id", [self.selected.client_id if self.selected else ""]
            )[0]
            code = values.get("code", [""])[0]
            if (
                not client_id.startswith("oaiapp_")
                or not code
                or len(code) > 4096
                or self.selected
                and client_id != self.selected.client_id
            ):
                raise ValidationError("Issued client identity required")
            tokens = self.http(
                "POST",
                TOKEN,
                body={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": code,
                    "code_verifier": self.verifier,
                    "redirect_uri": self.redirect,
                    "resource": RESOURCE,
                },
                form=True,
            )
            received_at = time.time()
            import jwt

            jwks = self.http("GET", JWKS)
            try:
                header = jwt.get_unverified_header(tokens["id_token"])
                if header.get("alg") != "RS256":
                    raise ValueError()
                keys = [
                    k
                    for k in jwks["keys"]
                    if k.get("kid") == header.get("kid")
                    and k.get("kty") == "RSA"
                    and k.get("use", "sig") == "sig"
                ]
                if len(keys) != 1:
                    raise ValueError()
                key = jwt.PyJWK.from_dict(keys[0], algorithm="RS256").key
                claims = jwt.decode(
                    tokens["id_token"],
                    key,
                    algorithms=["RS256"],
                    issuer=ISSUER,
                    audience=client_id,
                    leeway=5,
                    options={"require": ["sub", "exp", "iat", "nonce"]},
                )
                if (
                    not hmac.compare_digest(claims["nonce"], self.nonce)
                    or not isinstance(claims["sub"], str)
                    or not claims["sub"]
                ):
                    raise ValueError()
                if self.selected and claims["sub"] != self.selected.subject:
                    raise ValueError()
                scopes = tuple(sorted(set(tokens["scope"].split())))
                plan_enabled = {PLAN_SCOPE, "resource.invoke"} <= set(scopes)
                if "openid" not in scopes:
                    raise ValueError()
                lifetime = tokens.get(
                    "expires_in", max(0, min(3600, int(claims["exp"] - received_at)))
                )
                if (
                    type(lifetime) is not int
                    or not 0 <= lifetime <= 3600
                    or plan_enabled
                    and (
                        lifetime == 0
                        or tokens.get("token_type", "").lower() != "bearer"
                    )
                ):
                    raise ValueError()
                for k in ("access_token", "id_token", "refresh_token"):
                    if (
                        not isinstance(tokens.get(k, ""), str)
                        or len(tokens.get(k, "")) > 32000
                        or plan_enabled
                        and not tokens.get(k)
                    ):
                        raise ValueError()
            except Exception:
                raise Conflict(
                    "OAuth identity or plan grant validation failed"
                ) from None
            grant = Grant(
                client_id,
                claims["sub"],
                self.host_id,
                uuid.uuid4().hex,
                received_at + lifetime,
                scopes,
                (),
                tokens.get("access_token", ""),
                tokens.get("refresh_token", ""),
                tokens["id_token"],
            )
            # Preserve verified credentials/client registration on a temporary
            # catalog failure. An empty model set cannot authorize inference.
            self.vault.save(grant)
            if not plan_enabled:
                return grant
            catalog = self.http(
                "GET", RESOURCE + "/models", token=tokens["access_token"]
            )
            entries = catalog.get("models")
            if not isinstance(entries, list) or len(entries) > 200:
                raise Conflict("Invalid SIWC account model catalog")
            models = tuple(
                e["slug"]
                for e in entries
                if isinstance(e, dict)
                and e.get("visibility") == "list"
                and isinstance(e.get("slug"), str)
            )
            grant = replace(grant, models=models)
            self.vault.save(grant)
            return grant


def main():
    import argparse
    import webbrowser

    parser = argparse.ArgumentParser(
        description="Explicit II PRO Studio ChatGPT sign-in; no inference"
    )
    parser.add_argument("--sign-in", action="store_true", required=True)
    args = parser.parse_args()
    assert args.sign_in
    try:
        import jwt  # noqa: F401
        import cryptography  # noqa: F401
    except ImportError:
        parser.error(
            "Install requirements-siwc.lock in the project venv before sign-in"
        )
    root = Path(__file__).resolve().parents[1] / ".local/gpt-text-auth"
    vault = Vault(root)
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # OAuth callback contains one-time codes; never log URLs.

        def do_GET(self):
            uri = urlsplit(self.path)
            if (
                self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}"
                or uri.path != "/auth/callback"
                or len(self.path) > 16000
            ):
                self.send_error(400)
                return
            try:
                grant = pending.complete(uri.query)
                result["reference"] = grant.reference
                result["plan_enabled"] = grant.ready("gpt-6.2-sol")
                message = "Вход II PRO Studio завершён. Запрос GPT не выполнялся."
                status = 200
            except Exception:
                message = "Вход не подтверждён. Запрос GPT не выполнялся."
                status = 400
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(message.encode())

    with vault.login_lock(), HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 1
        pending = Authorization(
            vault,
            f"http://127.0.0.1:{server.server_port}/auth/callback",
            selected=vault.load(),
        )
        print(
            "Откроется Continue with ChatGPT для II PRO Studio. Проверьте план, лимит приложения и доступ к credits. Генерация не запускается.",
            flush=True,
        )
        webbrowser.open(pending.url())
        while not pending.used and time.monotonic() - pending.created < 300:
            server.handle_request()
    print(
        (
            "Вход подтверждён; генерация не запускалась."
            if result.get("plan_enabled")
            else "Вход сохранён, но разрешение на план/модель не подтверждено; генерация отключена."
        )
        if result
        else "Вход не подтверждён; автоматического повтора нет."
    )


if __name__ == "__main__":
    main()
