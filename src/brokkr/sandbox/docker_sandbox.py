"""Docker-backed command execution sandbox.

One long-lived container per "session" (not a fresh `docker run --rm` per
command) so a proposed sequence like "install a package, then use it"
works across calls. The container's rootfs is torn down and recreated --
never reused in place -- on reset(), so state never silently accumulates
across sessions in a way nobody can see or explain.

It also resets itself automatically after BROKKR_SANDBOX_IDLE_RESET_MINUTES
of no activity (0 or negative disables this), tracked via a small
timestamp file at Settings.sandbox_last_used_path rather than any Docker
metadata -- container labels aren't meant to be rewritten on every use,
and inspecting "when was this container created" answers a different
question than "when was it last actually used". This exists for the same
reason reset() exists at all: a sandbox someone walked away from hours
ago and forgot about is exactly the kind of silently-accumulated state
this module is designed never to have.

Two independent enforcement layers, deliberately not one:
  1. The Docker-level boundary (this module): a single scoped bind mount,
     --network none by default, resource limits, dropped capabilities,
     no-new-privileges, a non-root container user, and no Docker socket
     passthrough. This is what actually stops a catastrophic command from
     doing real damage.
  2. The static blocklist (permissions/policy.py) and, later, the
     human-approval flow -- defense in depth ON TOP of (1), applied at the
     point a command is *proposed* for approval, not here.

DockerSandbox.exec() is the raw mechanism and intentionally has NO
blocklist gate of its own. Stage 1's manual verification (see
~/.claude/plans/partitioned-fluttering-sonnet.md) deliberately runs things
like `rm -rf /` straight through this method, to prove layer (1) holds on
its own merits -- not because layer (2) caught it first.

Per-command timeout is enforced *inside* the container via GNU coreutils'
`timeout`, not via host-side thread/signal juggling: `docker exec` has no
native per-call timeout, and killing an exec'd process from the host
reliably needs its host-visible PID and matching privileges, which is a
much less robust mechanism than the same job done by a purpose-built
binary already running in the right namespace. The Docker client's own
HTTP timeout is set generously past the command timeout purely as a
backstop for a hung daemon/socket, not as the primary timeout mechanism.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.models.containers import Container

from brokkr.config import Settings

logger = logging.getLogger(__name__)

_DOCKERFILE_DIR = Path(__file__).resolve().parent
_CLIENT_TIMEOUT_BUFFER_SECONDS = 20.0
_TIMEOUT_EXIT_CODE = 124  # GNU `timeout`'s own convention on expiry
_MAX_STDOUT_BYTES = 200_000
_MAX_STDERR_BYTES = 50_000


class SandboxError(Exception):
    pass


@dataclass
class SandboxExecutionResult:
    command: list[str]
    exit_code: int
    timed_out: bool
    truncated: bool
    stdout: str
    stderr: str
    duration_ms: float
    container_id: str
    image_id: str


def _truncate(data: bytes, limit: int) -> tuple[str, bool]:
    if len(data) <= limit:
        return data.decode("utf-8", errors="replace"), False
    return data[:limit].decode("utf-8", errors="replace"), True


class DockerSandbox:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = docker.from_env(
            timeout=int(settings.sandbox.command_timeout_seconds + _CLIENT_TIMEOUT_BUFFER_SECONDS)
        )

    def build_image(self, force: bool = False) -> str:
        """Builds the sandbox image from sandbox/Dockerfile if it doesn't
        already exist (or unconditionally if force=True). Returns the
        image id."""
        image_name = self._settings.sandbox.image
        if not force:
            try:
                return self._client.images.get(image_name).id
            except ImageNotFound:
                pass

        logger.info("Building sandbox image %s from %s", image_name, _DOCKERFILE_DIR)
        image, _build_log = self._client.images.build(
            path=str(_DOCKERFILE_DIR),
            tag=image_name,
            rm=True,
        )
        return image.id

    def _get_container(self) -> Container | None:
        try:
            return self._client.containers.get(self._settings.sandbox.container_name)
        except NotFound:
            return None

    def existing_container(self) -> Container | None:
        """Returns the sandbox container if one exists, without creating
        or starting it -- for status/introspection callers that shouldn't
        trigger a build+run just to check."""
        return self._get_container()

    def _read_last_used(self) -> datetime | None:
        try:
            raw = self._settings.sandbox_last_used_path.read_text().strip()
            return datetime.fromisoformat(raw)
        except (FileNotFoundError, ValueError):
            return None

    def _touch_last_used(self) -> None:
        path = self._settings.sandbox_last_used_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(timezone.utc).isoformat())

    def _is_idle_expired(self) -> bool:
        idle_minutes = self._settings.sandbox.idle_reset_minutes
        if idle_minutes <= 0:
            return False
        last_used = self._read_last_used()
        if last_used is None:
            # No recorded use yet (e.g. the container predates this
            # feature, or the marker file was cleared) -- don't reset
            # something that's never actually been used idly.
            return False
        elapsed_minutes = (datetime.now(timezone.utc) - last_used).total_seconds() / 60
        return elapsed_minutes >= idle_minutes

    def ensure_running(self) -> Container:
        """Returns the long-lived sandbox container, starting or creating
        it if necessary. Auto-resets it first if it's been idle past
        BROKKR_SANDBOX_IDLE_RESET_MINUTES; otherwise does NOT reset an
        existing container's rootfs -- call reset() explicitly for that."""
        container = self._get_container()
        if container is not None and self._is_idle_expired():
            logger.info(
                "Sandbox idle past %.0f minutes, resetting before reuse",
                self._settings.sandbox.idle_reset_minutes,
            )
            self.reset()
            container = None

        if container is not None:
            container.reload()
            if container.status != "running":
                container.start()
            return container

        self.build_image()
        sandbox = self._settings.sandbox
        self._touch_last_used()
        return self._client.containers.run(
            sandbox.image,
            name=sandbox.container_name,
            detach=True,
            network_mode=sandbox.network,
            mem_limit=sandbox.memory_limit,
            nano_cpus=int(sandbox.cpu_limit * 1_000_000_000),
            pids_limit=sandbox.pids_limit,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            privileged=False,
            # Overrides the Dockerfile's own `USER brokkr` (uid 10001) --
            # that fixed uid has no write access to workdir_host, which is
            # owned by whichever host account runs brokkr. Running as that
            # same uid/gid instead means the mount is actually writable AND
            # files created in it come out owned by a real host user
            # instead of an orphaned container-only uid. Verified during
            # Stage 1 manual testing: uid 10001 got "Permission denied" on
            # a plain `touch` inside /workspace before this fix.
            user=f"{os.getuid()}:{os.getgid()}",
            # The host uid above has no matching /etc/passwd entry inside
            # the container (only root and the image's own uid 10001 do),
            # so without this, $HOME defaults to "/" -- unwritable, which
            # broke `pip`'s cache (a warning) and `git config --global`
            # (a hard "Permission denied" failure), both found by manually
            # dogfooding real tasks through `brokkr propose` after Stage 3.
            environment={"HOME": sandbox.workdir_container},
            volumes={
                str(sandbox.workdir_host): {
                    "bind": sandbox.workdir_container,
                    "mode": "rw",
                }
            },
            working_dir=sandbox.workdir_container,
        )

    def reset(self) -> None:
        """Stops and removes the sandbox container so the next
        ensure_running() call creates it fresh -- new rootfs, nothing
        carried over from before."""
        self._settings.sandbox_last_used_path.unlink(missing_ok=True)
        container = self._get_container()
        if container is None:
            return
        logger.info("Resetting sandbox container %s", container.id[:12])
        container.stop(timeout=5)
        container.remove(force=True)

    def exec(self, argv: list[str], timeout: float | None = None) -> SandboxExecutionResult:
        """Runs argv inside the sandbox container. argv is passed straight
        through to Docker's exec API as a list -- never joined into a
        shell string, never interpreted by a shell inside the container
        unless argv itself explicitly names one (e.g. ["bash", "-c",
        ...]), matching the argv-only discipline used throughout brokkr."""
        if not argv:
            raise ValueError("argv must not be empty")

        sandbox = self._settings.sandbox
        effective_timeout = timeout if timeout is not None else sandbox.command_timeout_seconds
        container = self.ensure_running()

        wrapped = [
            "timeout",
            "--kill-after=2s",
            "--signal=TERM",
            f"{effective_timeout}s",
            *argv,
        ]

        started = time.monotonic()
        try:
            exit_code, (stdout_bytes, stderr_bytes) = container.exec_run(
                wrapped,
                workdir=sandbox.workdir_container,
                demux=True,
            )
        except APIError as exc:
            raise SandboxError(f"docker exec failed: {exc}") from exc
        duration_ms = (time.monotonic() - started) * 1000
        self._touch_last_used()

        stdout, stdout_truncated = _truncate(stdout_bytes or b"", _MAX_STDOUT_BYTES)
        stderr, stderr_truncated = _truncate(stderr_bytes or b"", _MAX_STDERR_BYTES)

        return SandboxExecutionResult(
            command=argv,
            exit_code=exit_code,
            timed_out=exit_code == _TIMEOUT_EXIT_CODE,
            truncated=stdout_truncated or stderr_truncated,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            container_id=container.id,
            image_id=container.image.id,
        )
