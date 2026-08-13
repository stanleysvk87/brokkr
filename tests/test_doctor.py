from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from docker.errors import APIError, DockerException
from typer.testing import CliRunner

from brokkr import cli, doctor
from brokkr.config import SandboxConfig, Settings
from brokkr.doctor import DoctorCheck, DoctorReport, LocalModel


@pytest.fixture
def settings(tmp_path) -> Settings:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Settings(
        ollama_url="http://127.0.0.1:11434",
        default_model="configured-model:7b",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        log_level="INFO",
        sandbox=SandboxConfig(
            image="configured-image:latest",
            workdir_host=workspace,
        ),
        approval_template_matching=False,
    )


class FakeResponse:
    def __init__(self, models=None, *, error: httpx.HTTPError | None = None):
        self._models = models if models is not None else []
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        return {"models": self._models}


def _install_docker(monkeypatch, *, ping=True, image_exists=True):
    class FakeSandbox:
        def __init__(self, settings):
            pass

        def ping(self):
            if isinstance(ping, Exception):
                raise ping
            return ping

        def image_exists(self):
            if isinstance(image_exists, Exception):
                raise image_exists
            return image_exists

    monkeypatch.setattr(doctor, "DockerSandbox", FakeSandbox)


def _install_ollama(monkeypatch, models=None, *, error=None):
    monkeypatch.setattr(
        doctor.httpx,
        "get",
        lambda url, timeout: FakeResponse(models, error=error),
    )


def _check(report: DoctorReport, name: str) -> DoctorCheck:
    return next(check for check in report.checks if check.name == name)


def test_all_checks_pass_and_models_are_returned(monkeypatch, settings):
    _install_docker(monkeypatch)
    _install_ollama(
        monkeypatch,
        [{"name": "configured-model:7b", "size": 4_700_000_000}],
    )

    report = doctor.run_doctor(settings)

    assert [check.status for check in report.checks] == ["pass"] * 5
    assert report.models == [LocalModel("configured-model:7b", 4_700_000_000)]
    assert report.failed is False


def test_docker_failure_does_not_skip_ollama_or_workspace(monkeypatch, settings):
    _install_docker(monkeypatch, ping=DockerException("unreachable"))
    _install_ollama(monkeypatch, [{"name": "configured-model:7b", "size": 1}])

    report = doctor.run_doctor(settings)

    assert _check(report, "Docker").status == "fail"
    assert _check(report, "Sandbox image").status == "warn"
    assert _check(report, "Ollama").status == "pass"
    assert _check(report, "Workspace").status == "pass"


def test_false_docker_ping_is_a_failure(monkeypatch, settings):
    _install_docker(monkeypatch, ping=False)
    _install_ollama(monkeypatch, [{"name": "configured-model:7b"}])

    report = doctor.run_doctor(settings)

    assert _check(report, "Docker").status == "fail"
    assert _check(report, "Sandbox image").status == "warn"


def test_missing_sandbox_image_is_only_a_warning(monkeypatch, settings):
    _install_docker(monkeypatch, image_exists=False)
    _install_ollama(monkeypatch, [{"name": "configured-model:7b"}])

    report = doctor.run_doctor(settings)

    image = _check(report, "Sandbox image")
    assert image.status == "warn"
    assert "configured-image:latest" in image.message
    assert report.failed is False


def test_image_inspection_error_is_a_failure(monkeypatch, settings):
    _install_docker(monkeypatch, image_exists=APIError("inspection failed"))
    _install_ollama(monkeypatch, [{"name": "configured-model:7b"}])

    report = doctor.run_doctor(settings)

    assert _check(report, "Sandbox image").status == "fail"


def test_ollama_failure_warns_that_model_was_not_checked(monkeypatch, settings):
    _install_docker(monkeypatch)
    request = httpx.Request("GET", "http://127.0.0.1:11434/api/tags")
    _install_ollama(monkeypatch, error=httpx.ConnectError("refused", request=request))

    report = doctor.run_doctor(settings)

    ollama = _check(report, "Ollama")
    assert ollama.status == "fail"
    assert settings.ollama_url in ollama.message
    assert "BROKKR_OLLAMA_URL" in ollama.message
    assert _check(report, "Default model").status == "warn"
    assert _check(report, "Workspace").status == "pass"


def test_missing_model_message_is_exact_and_actionable(monkeypatch, settings):
    _install_docker(monkeypatch)
    _install_ollama(monkeypatch, [{"name": "another-model:3b", "size": 10}])

    report = doctor.run_doctor(settings)

    model = _check(report, "Default model")
    assert model.status == "fail"
    assert model.message == (
        "model 'configured-model:7b' is not pulled -- "
        "run: ollama pull configured-model:7b"
    )


def test_missing_workspace_is_a_failure(monkeypatch, settings):
    _install_docker(monkeypatch)
    _install_ollama(monkeypatch, [{"name": "configured-model:7b"}])
    settings = settings.model_copy(
        update={
            "sandbox": settings.sandbox.model_copy(
                update={"workdir_host": settings.sandbox.workdir_host / "missing"}
            )
        }
    )

    report = doctor.run_doctor(settings)

    assert _check(report, "Workspace").status == "fail"
    assert "does not exist" in _check(report, "Workspace").message


def test_non_writable_workspace_is_a_failure(monkeypatch, settings):
    _install_docker(monkeypatch)
    _install_ollama(monkeypatch, [{"name": "configured-model:7b"}])
    monkeypatch.setattr(doctor.os, "access", lambda path, mode: False)

    report = doctor.run_doctor(settings)

    assert _check(report, "Workspace").status == "fail"
    assert "not writable" in _check(report, "Workspace").message


def _report(*statuses: str) -> DoctorReport:
    return DoctorReport(
        checks=[
            DoctorCheck(f"check-{index}", status, f"message-{index}")
            for index, status in enumerate(statuses)
        ],
        models=[LocalModel("local-model:7b", 4_500_000_000)],
    )


def test_cli_returns_zero_when_all_checks_pass(monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "run_doctor", lambda settings: _report("pass", "pass"))

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Summary: 2 passed, 0 warnings, 0 failed" in result.output
    assert "local-model:7b" in result.output


def test_cli_returns_zero_for_warning_only(monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "run_doctor", lambda settings: _report("pass", "warn"))

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Summary: 1 passed, 1 warning, 0 failed" in result.output


def test_cli_returns_nonzero_when_any_check_fails(monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "run_doctor", lambda settings: _report("pass", "fail"))

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 1
    assert "Summary: 1 passed, 0 warnings, 1 failed" in result.output
