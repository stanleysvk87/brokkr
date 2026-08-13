"""Opt-in real-Docker verification for every shipped library seed entry."""

from __future__ import annotations

import os
import tarfile
import zipfile
from pathlib import Path

import pytest
from docker.errors import DockerException, NotFound

from brokkr.approvals.store import ApprovalStore
from brokkr.config import SandboxConfig, Settings
from brokkr.permissions.policy import check_prohibited
from brokkr.sandbox.docker_sandbox import DockerSandbox

pytestmark = pytest.mark.skipif(
    os.environ.get("BROKKR_RUN_DOCKER_TESTS") != "1",
    reason="set BROKKR_RUN_DOCKER_TESTS=1 to run Docker integration tests",
)

_IMAGE = "brokkr-library-seed-test:latest"
_CONTAINER = "brokkr-library-seed-test"


def _write_minimal_pdf(path: Path, text: str) -> None:
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode())
        pdf.extend(body)
        pdf.extend(b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    path.write_bytes(pdf)


@pytest.fixture(scope="module")
def library_sandbox(tmp_path_factory):
    root = tmp_path_factory.mktemp("library-seeds")
    workspace = root / "workspace"
    workspace.mkdir()
    settings = Settings(
        ollama_url="http://127.0.0.1:11434",
        default_model="test-model",
        data_dir=root / "state",
        log_dir=root / "logs",
        log_level="INFO",
        sandbox=SandboxConfig(
            image=_IMAGE,
            container_name=_CONTAINER,
            workdir_host=workspace,
            idle_reset_minutes=0,
        ),
        approval_template_matching=False,
    )

    (workspace / "data").mkdir()
    (workspace / "data" / "note.txt").write_text("archive seed\n")
    (workspace / "input.txt").write_text("TODO first\ndone\nTODO second\n")
    with (workspace / "huge.bin").open("wb") as handle:
        handle.seek(101 * 1024 * 1024)
        handle.write(b"\0")
    _write_minimal_pdf(workspace / "document.pdf", "BROKKR OCR TEST")
    archive_source = root / "archived.txt"
    archive_source.write_text("tar extracted\n")
    with tarfile.open(workspace / "archive.tar.gz", "w:gz") as archive:
        archive.add(archive_source, arcname="archived.txt")
    with zipfile.ZipFile(workspace / "archive.zip", "w") as archive:
        archive.writestr("zipped.txt", "zip extracted\n")
    (workspace / "repo").mkdir()

    sandbox = DockerSandbox(settings)
    sandbox.reset()
    sandbox.build_image(force=True)
    assert sandbox.exec(["git", "-C", "/workspace/repo", "init"]).exit_code == 0
    (workspace / "repo" / "untracked.txt").write_text("git status seed\n")
    rendered = sandbox.exec(
        [
            "pdftoppm",
            "-f",
            "1",
            "-singlefile",
            "-png",
            "/workspace/document.pdf",
            "/workspace/scan",
        ]
    )
    assert rendered.exit_code == 0, rendered.stderr

    try:
        yield ApprovalStore(settings), sandbox, workspace
    finally:
        sandbox.reset()
        try:
            sandbox._client.networks.get(f"{_CONTAINER}-internal").remove()
        except (DockerException, NotFound):
            pass
        try:
            sandbox._client.images.remove(_IMAGE, force=True)
        except DockerException:
            pass


@pytest.mark.parametrize(
    "name",
    [
        "workspace-disk-usage",
        "find-large-files",
        "find-recent-files",
        "archive-workspace",
        "extract-tar-archive",
        "extract-zip-archive",
        "count-todo-lines",
        "extract-pdf-text",
        "ocr-scanned-image",
        "git-worktree-status",
    ],
)
def test_seed_entry_runs_against_real_files(library_sandbox, name):
    store, sandbox, workspace = library_sandbox
    entry = store.get_library_entry(name)
    assert entry is not None
    assert check_prohibited(entry.argv) is None

    result = sandbox.exec(entry.argv)

    assert result.exit_code == 0, result.stderr
    if name == "workspace-disk-usage":
        assert "/workspace" in result.stdout
    elif name == "find-large-files":
        assert "/workspace/huge.bin" in result.stdout
    elif name == "find-recent-files":
        assert "/workspace/input.txt" in result.stdout
    elif name == "archive-workspace":
        assert (workspace / "workspace.tar.gz").is_file()
        with tarfile.open(workspace / "workspace.tar.gz", "r:gz") as archive:
            assert "./data/note.txt" in archive.getnames()
            assert "./workspace.tar.gz" not in archive.getnames()
    elif name == "extract-tar-archive":
        assert (workspace / "extracted-tar" / "archived.txt").read_text() == "tar extracted\n"
    elif name == "extract-zip-archive":
        assert (workspace / "extracted-zip" / "zipped.txt").read_text() == "zip extracted\n"
    elif name == "count-todo-lines":
        assert result.stdout.strip() == "2"
    elif name == "extract-pdf-text":
        assert "BROKKR OCR TEST" in (workspace / "document.txt").read_text()
    elif name == "ocr-scanned-image":
        assert "BROKKR OCR TEST" in (workspace / "ocr.txt").read_text()
    else:
        assert "untracked.txt" in result.stdout
