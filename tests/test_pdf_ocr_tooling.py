"""Opt-in Docker integration coverage for PDF extraction and OCR tooling.

The normal unit suite does not require a Docker daemon. Run this module on a
Docker-capable development host with BROKKR_RUN_DOCKER_TESTS=1.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from docker.errors import DockerException, NotFound

from brokkr.config import SandboxConfig, Settings
from brokkr.sandbox.docker_sandbox import DockerSandbox

pytestmark = pytest.mark.skipif(
    os.environ.get("BROKKR_RUN_DOCKER_TESTS") != "1",
    reason="set BROKKR_RUN_DOCKER_TESTS=1 to run Docker integration tests",
)

_KNOWN_TEXT = "BROKKR PDF EXTRACTION OK"
_IMAGE = "brokkr-pdf-ocr-test:latest"
_CONTAINER = "brokkr-pdf-ocr-test"


def _write_minimal_pdf(path: Path, text: str) -> None:
    """Write a dependency-free, one-page PDF with a real text layer."""
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
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
def real_sandbox(tmp_path_factory):
    root = tmp_path_factory.mktemp("pdf-ocr-sandbox")
    workspace = root / "workspace"
    workspace.mkdir()
    settings = Settings(
        ollama_url="http://127.0.0.1:11434",
        default_model="test-model",
        data_dir=root / "data",
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
    sandbox = DockerSandbox(settings)
    sandbox.reset()
    sandbox.build_image(force=True)
    try:
        yield sandbox, workspace
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


def test_pdf_and_ocr_binaries_run(real_sandbox):
    sandbox, _workspace = real_sandbox

    pdftotext = sandbox.exec(["pdftotext", "-v"])
    tesseract = sandbox.exec(["tesseract", "--version"])

    assert pdftotext.exit_code == 0
    assert "pdftotext version" in pdftotext.stderr
    assert tesseract.exit_code == 0
    assert "tesseract" in tesseract.stdout.lower()


def test_pdftotext_extracts_known_text_from_real_pdf(real_sandbox):
    sandbox, workspace = real_sandbox
    _write_minimal_pdf(workspace / "sample.pdf", _KNOWN_TEXT)

    result = sandbox.exec(
        ["pdftotext", "/workspace/sample.pdf", "/workspace/extracted.txt"]
    )

    assert result.exit_code == 0, result.stderr
    assert _KNOWN_TEXT in (workspace / "extracted.txt").read_text()
