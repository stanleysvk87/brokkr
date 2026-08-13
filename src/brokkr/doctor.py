"""Read-only setup and health diagnostics for brokkr."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

import httpx
from docker.errors import DockerException

from brokkr.config import Settings
from brokkr.sandbox.docker_sandbox import DockerSandbox

CheckStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    message: str


@dataclass(frozen=True)
class LocalModel:
    name: str
    size_bytes: int | None


@dataclass(frozen=True)
class DoctorReport:
    checks: list[DoctorCheck]
    models: list[LocalModel]

    @property
    def failed(self) -> bool:
        return any(check.status == "fail" for check in self.checks)


def _docker_checks(settings: Settings) -> list[DoctorCheck]:
    try:
        sandbox = DockerSandbox(settings)
        if not sandbox.ping():
            raise DockerException("Docker ping returned false")
    except DockerException:
        return [
            DoctorCheck(
                "Docker",
                "fail",
                "Docker daemon not reachable -- is Docker running? "
                "Is your user in the docker group?",
            ),
            DoctorCheck(
                "Sandbox image",
                "warn",
                "not checked because Docker is not reachable",
            ),
        ]

    reachable = DoctorCheck("Docker", "pass", "Docker daemon is reachable")
    try:
        image_exists = sandbox.image_exists()
    except DockerException:
        image_check = DoctorCheck(
            "Sandbox image",
            "fail",
            f"could not inspect local image '{settings.sandbox.image}'",
        )
    else:
        image_check = (
            DoctorCheck(
                "Sandbox image",
                "pass",
                f"local image '{settings.sandbox.image}' is present",
            )
            if image_exists
            else DoctorCheck(
                "Sandbox image",
                "warn",
                f"local image '{settings.sandbox.image}' is not built yet; "
                "brokkr will build it on first sandbox use",
            )
        )
    return [reachable, image_check]


def _ollama_checks(settings: Settings) -> tuple[list[DoctorCheck], list[LocalModel]]:
    tags_url = f"{settings.ollama_url.rstrip('/')}/api/tags"
    try:
        response = httpx.get(tags_url, timeout=5.0)
        response.raise_for_status()
        payload = response.json()
        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            raise TypeError("models is not a list")
    except (httpx.HTTPError, TypeError, ValueError, AttributeError):
        return (
            [
                DoctorCheck(
                    "Ollama",
                    "fail",
                    f"Ollama not reachable at {settings.ollama_url} -- is it running? "
                    "Is BROKKR_OLLAMA_URL correct?",
                ),
                DoctorCheck(
                    "Default model",
                    "warn",
                    "not checked because Ollama is not reachable",
                ),
            ],
            [],
        )

    models = [
        LocalModel(
            name=model["name"],
            size_bytes=model.get("size") if isinstance(model.get("size"), int) else None,
        )
        for model in raw_models
        if isinstance(model, dict) and isinstance(model.get("name"), str)
    ]
    available_names = {model.name for model in models}
    model_check = (
        DoctorCheck(
            "Default model",
            "pass",
            f"configured model '{settings.default_model}' is available",
        )
        if settings.default_model in available_names
        else DoctorCheck(
            "Default model",
            "fail",
            f"model '{settings.default_model}' is not pulled -- "
            f"run: ollama pull {settings.default_model}",
        )
    )
    return [DoctorCheck("Ollama", "pass", f"reachable at {settings.ollama_url}"), model_check], models


def _workspace_check(settings: Settings) -> DoctorCheck:
    workspace = settings.sandbox.workdir_host
    if not workspace.is_dir():
        return DoctorCheck(
            "Workspace",
            "fail",
            f"workspace directory does not exist: {workspace}",
        )

    # os.access avoids leaving probe files behind. It reflects effective access
    # for this process, though unusual ACL/network filesystems can still differ.
    if not os.access(workspace, os.W_OK):
        return DoctorCheck(
            "Workspace",
            "fail",
            f"workspace directory is not writable: {workspace}",
        )
    return DoctorCheck("Workspace", "pass", f"directory is writable: {workspace}")


def run_doctor(settings: Settings) -> DoctorReport:
    """Run every diagnostic without building, starting, or executing anything."""
    checks = _docker_checks(settings)
    ollama_checks, models = _ollama_checks(settings)
    checks.extend(ollama_checks)
    checks.append(_workspace_check(settings))
    return DoctorReport(checks=checks, models=models)
