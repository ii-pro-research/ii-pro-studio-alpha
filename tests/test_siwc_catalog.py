import io
import json

import pytest

from ii_pro_core.errors import Conflict
from ii_pro_team import siwc
from test_siwc import callback, flow


pytestmark = pytest.mark.usefixtures("siwc_protocol_vault")

def payload(size):
    base = json.dumps(
        {
            "models": [
                {
                    "slug": "gpt-6.2-sol",
                    "display_name": "GPT-6.2-Sol",
                    "visibility": "list",
                }
            ],
            "padding": "",
        }
    ).encode()
    return base[:-2] + b"x" * (size - len(base)) + base[-2:]


def connection(monkeypatch, raw):
    class Reply(io.BytesIO):
        status = 200

        def read(self, size):
            self.read_size = size
            return super().read(size)

    reply = Reply(raw)
    calls = []

    class Connection:
        def __init__(self, host, timeout):
            self.closed = False

        def request(self, method, path, **kwargs):
            calls.append((method, path))

        def getresponse(self):
            return reply

        def close(self):
            self.closed = True

    monkeypatch.setattr(siwc, "HTTPSConnection", Connection)
    return reply, calls


@pytest.mark.parametrize("size", [850906, siwc.CATALOG_MAX_BYTES])
def test_bounded_siwc_catalog_accepts_large_valid_metadata(monkeypatch, size):
    raw = payload(size)
    assert len(raw) == size
    reply, calls = connection(monkeypatch, raw)
    value = siwc.request("GET", siwc.RESOURCE + "/models", token="synthetic")
    assert value["models"][0]["slug"] == "gpt-6.2-sol"
    assert reply.read_size == siwc.CATALOG_MAX_BYTES + 1
    assert calls == [("GET", "/v1/models")]


def test_catalog_larger_than_2_mib_is_rejected_without_retry(monkeypatch):
    reply, calls = connection(monkeypatch, payload(siwc.CATALOG_MAX_BYTES + 1))
    with pytest.raises(Conflict):
        siwc.request("GET", siwc.RESOURCE + "/models", token="synthetic")
    assert reply.tell() == siwc.CATALOG_MAX_BYTES + 1
    assert len(calls) == 1


@pytest.mark.parametrize(
    "method,url",
    [("POST", siwc.TOKEN), ("GET", siwc.JWKS), ("POST", siwc.RESOURCE + "/models")],
)
def test_larger_catalog_quota_does_not_widen_other_requests(monkeypatch, method, url):
    reply, calls = connection(monkeypatch, payload(850906))
    with pytest.raises(Conflict):
        siwc.request(method, url)
    assert reply.read_size == siwc.METADATA_MAX_BYTES + 1
    assert len(calls) == 1


@pytest.mark.parametrize(
    "catalog",
    [{"data": [{"id": "gpt-6.2-sol"}]}, {"models": {}}, {"models": [{}] * 201}],
)
def test_siwc_still_rejects_ordinary_api_shape_and_entry_overflow(tmp_path, catalog):
    issuer, vault, pending = flow(tmp_path)

    def http(method, url, **kwargs):
        if url == siwc.RESOURCE + "/models":
            return catalog
        return issuer.http(method, url, **kwargs)

    pending.http = http
    with pytest.raises(Conflict, match="Invalid SIWC account model catalog"):
        pending.complete(callback(pending, iss=siwc.ISSUER))
    assert vault.load().models == ()
