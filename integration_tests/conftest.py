import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pytest_addoption(parser):
    parser.addoption("--docker-image", help="Explicit locally built immutable image ID")
    parser.addoption("--docker-review", help="Deployment review digest explicitly approved by the operator")


@pytest.fixture(scope="session")
def docker_settings(request):
    from ii_pro_worker.acceptance import proposed_review
    from ii_pro_worker.profile import DockerProfile
    image, review = request.config.getoption("--docker-image"), request.config.getoption("--docker-review")
    if not image or review != proposed_review()["review_digest"]:
        pytest.fail("Live Docker tests require explicit operator approval and a pinned approved build")
    return DockerProfile(image), review
