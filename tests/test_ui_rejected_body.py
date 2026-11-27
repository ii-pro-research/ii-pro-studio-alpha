from email.message import Message
import io

import pytest

from ii_pro_ui.server import Handler


class Socket:
    def __init__(self):
        self.timeout = 5.0
        self.values = []

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.values.append(value)
        self.timeout = value


def handler(size):
    value = Handler.__new__(Handler)
    value.headers = Message()
    value.headers["Content-Length"] = str(size)
    value.rfile = io.BytesIO(b"x" * size)
    value.connection = Socket()
    return value


@pytest.mark.parametrize("size", [4096, 4097, 8192, 8193])
def test_rejection_drain_is_bounded_and_does_not_expand_command_limit(size):
    value = handler(size)
    value.discard_small_body()
    assert value.rfile.tell() == (size if size <= 8192 else 0)
    assert value.connection.timeout == 5.0
    if size <= 8192:
        assert value.connection.values == [1.0, 5.0]


@pytest.mark.parametrize("ambiguous", ["duplicate", "chunked"])
def test_rejection_never_drains_ambiguous_framing(ambiguous):
    value = handler(4097)
    if ambiguous == "duplicate":
        value.headers["Content-Length"] = "4097"
    else:
        value.headers["Transfer-Encoding"] = "chunked"
    value.discard_small_body()
    assert value.rfile.tell() == 0 and not value.connection.values


def test_rejection_read_timeout_is_restored_and_never_dispatches():
    value = handler(4097)

    class Slow:
        def read(self, size):
            assert size == 4097 and value.connection.timeout == 1.0
            raise TimeoutError("synthetic stalled sender")

    value.rfile = Slow()
    value.discard_small_body()
    assert value.connection.values == [1.0, 5.0]
