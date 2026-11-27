"""Only developer-owned fixed laboratory files. No model-controlled paths."""

import ast
import difflib
import hashlib
import inspect
import os
from pathlib import Path
import shutil
import subprocess

from ii_pro_core.errors import Conflict, ValidationError
from ii_pro_core.security import no_links, safe_root
from .protocol import identity

BEFORE = 'def slug_words(text):\n    return "-".join(text.split())\n'
AFTER = 'def slug_words(text):\n    return "-".join(text.lower().split())\n'
TEST_SOURCE = 'from solution import slug_words\n\ndef test_slug_words():\n    assert slug_words("  Hello   WORLD ") == "hello-world"\n'
CONTRACT = "В лабораторном примере slug_words преобразует пробелы в дефисы и приводит текст к нижнему регистру. Проверить пустую строку, смешанный регистр и повторные пробелы."


def slug_words(text):
    return "-".join(text.lower().split())


def fingerprint(data):
    return hashlib.sha256(data).hexdigest()


class Laboratory:
    def __init__(self, root):
        self.root = safe_root(Path(root))
        selected = shutil.which("git")
        if not selected:
            raise ValidationError("Git is required for the registered laboratory")
        self.git_executable = Path(selected).resolve(strict=True)
        no_links(self.git_executable)
        self.git_sha256 = fingerprint(self.git_executable.read_bytes())

    def area(self, job_id, role):
        identity(job_id)
        if role not in {"architect", "engineer", "qwen"}:
            raise ValidationError("Unregistered laboratory role")
        path = self.root / job_id / role
        no_links(path)
        return safe_root(path)

    def _file(self, directory, name, content):
        path = directory / name
        no_links(path)
        if path.exists():
            if path.read_bytes() != content:
                raise Conflict("Laboratory file differs from its frozen fixture")
        else:
            with path.open("xb") as stream:
                stream.write(content)

    def seed(self, job_id):
        for role in ("architect", "engineer", "qwen"):
            directory = self.area(job_id, role)
            # Engineer may already have a completed checkpoint after restart.
            source = directory / "solution.py"
            no_links(source)
            if (
                role == "engineer"
                and source.exists()
                and source.read_bytes() == AFTER.encode()
            ):
                continue
            self._file(directory, "solution.py", BEFORE.encode())
        return self.area(job_id, "engineer")

    def git(self, directory, *arguments):
        no_links(directory)
        no_links(directory / ".git")
        binary = self.git_executable
        no_links(binary)
        if fingerprint(binary.read_bytes()) != self.git_sha256:
            raise Conflict("Git executable changed after laboratory registration")
        # No user hooks, credentials, inherited GIT_DIR, aliases or project config.
        env = {
            k: os.environ[k]
            for k in ("PATH", "SystemRoot", "TEMP", "TMP")
            if k in os.environ
        }
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
        )
        result = subprocess.run(
            [
                binary,
                "-c",
                "core.hooksPath=" + str(self.root / "disabled-hooks"),
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "user.name=II PRO Mock Laboratory",
                "-c",
                "user.email=lab@localhost",
                *arguments,
            ],
            cwd=directory,
            env=env,
            shell=False,
            capture_output=True,
            timeout=10,
        )
        if (
            result.returncode
            or len(result.stdout) > 16000
            or len(result.stderr) > 16000
        ):
            raise Conflict("Fixed laboratory Git operation failed")
        return result.stdout.decode("utf-8", errors="replace").strip()

    def implement(self, job_id):
        directory = self.seed(job_id)
        if (directory / ".git").exists():
            raise Conflict(
                "An existing laboratory Git history requires inspection; no automatic replay"
            )
        self.git(directory, "init", "-q", "--initial-branch=team/mock-v1")
        self.git(directory, "add", "--", "solution.py")
        self.git(directory, "commit", "-q", "-m", "Developer laboratory baseline")
        before = self.git(directory, "rev-parse", "HEAD")
        path = directory / "solution.py"
        no_links(path)
        if path.read_bytes() != BEFORE.encode():
            raise Conflict("Baseline changed")
        path.write_bytes(AFTER.encode())
        self._file(directory, "test_solution.py", TEST_SOURCE.encode())
        observed = self.verify(job_id)
        diff = (
            self.git(
                directory, "diff", "--no-ext-diff", "--no-textconv", "--", "solution.py"
            )
            + "\n"
            + "\n".join(
                difflib.unified_diff(
                    [],
                    TEST_SOURCE.splitlines(),
                    fromfile="/dev/null",
                    tofile="test_solution.py",
                    lineterm="",
                )
            )
        )
        self.git(directory, "add", "--", "solution.py", "test_solution.py")
        self.git(
            directory, "commit", "-q", "-m", "Developer fixture: lowercase slug words"
        )
        return {
            **observed,
            "diff": diff,
            "git_before": before,
            "git_after": self.git(directory, "rev-parse", "HEAD"),
            "workspace": "engineer",
        }

    def verify(self, job_id):
        if ast.dump(ast.parse(AFTER)) != ast.dump(
            ast.parse(inspect.getsource(slug_words))
        ):
            raise Conflict(
                "Workspace fixture differs from the installed trusted reference"
            )
        directory = self.area(job_id, "engineer")
        for name, expected in (
            ("solution.py", AFTER),
            ("test_solution.py", TEST_SOURCE),
        ):
            no_links(directory / name)
            if (directory / name).read_bytes() != expected.encode():
                raise Conflict(
                    "Developer fixture identity changed; no import or execution"
                )
            ast.parse(expected)
        cases = [
            ("empty", "", ""),
            ("case", "Hello WORLD", "hello-world"),
            ("spaces", "  Hello   WORLD ", "hello-world"),
            ("unicode", "ПРИВЕТ Мир", "привет-мир"),
        ]
        tests = [
            {"id": key, "passed": slug_words(text) == expected}
            for key, text, expected in cases
        ]
        return {
            "verification": "developer_reference_and_exact_file_bytes",
            "tests": tests,
            "source_sha256": fingerprint(AFTER.encode()),
            "test_sha256": fingerprint(TEST_SOURCE.encode()),
            "workspace_code_executed": False,
            "model_generated_code": False,
        }

    def review(self, job_id, observed):
        verified = self.verify(job_id)
        directory = self.area(job_id, "engineer")
        if self.git(directory, "rev-parse", "HEAD") != observed[
            "git_after"
        ] or self.git(directory, "status", "--porcelain"):
            raise Conflict("Git checkpoint changed after Engineer")
        if any(observed.get(k) != verified[k] for k in verified):
            raise Conflict("Test evidence differs from the reference verification")
        return verified
