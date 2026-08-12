"""Locks in permissions/policy.py's blocklist behavior -- this is the
one piece of brokkr where a false negative is a real safety issue, so
its exact matched/not-matched boundaries deserve explicit test coverage
rather than only manual verification."""

from __future__ import annotations

import pytest

from brokkr.permissions.policy import check_prohibited


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-rf", "/"],
        ["rm", "-rf", "/*"],
        ["rm", "-fr", "~"],
        ["dd", "if=/dev/zero", "of=/dev/sda"],
        ["dd", "if=/dev/zero", "of=/dev/nvme0n1"],
        ["mkfs.ext4", "/dev/sdb1"],
        ["chmod", "-R", "777", "/"],
        ["bash", "-c", ":(){ :|:& };:"],
    ],
)
def test_blocks_known_catastrophic_patterns(argv):
    assert check_prohibited(argv) is not None


@pytest.mark.parametrize(
    "argv",
    [
        ["ls", "-la", "/workspace"],
        ["rm", "myfile.txt"],
        ["rm", "-rf", "/workspace/scratch"],
        ["dd", "if=/dev/zero", "of=/workspace/testfile", "bs=1M", "count=1"],
        ["chmod", "-R", "755", "/workspace/project"],
        ["git", "clone", "https://example.com/repo.git"],
        ["python3", "script.py"],
    ],
)
def test_allows_ordinary_commands(argv):
    assert check_prohibited(argv) is None


def test_empty_argv_is_not_prohibited():
    assert check_prohibited([]) is None
