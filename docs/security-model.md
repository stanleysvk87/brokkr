# Security model

## The actual boundary

The Docker sandbox — specifically, four properties of the container
`brokkr/sandbox/docker_sandbox.py` creates — is the real safety boundary.
Everything else in this project (the policy blocklist, human review, the
approval store) is defense in depth *on top of* this boundary, not a
substitute for it. This was verified manually, not just asserted; see
"What was actually tested" below.

1. **A single bind mount, nothing else.** The container sees exactly one
   host directory (`BROKKR_SANDBOX_WORKDIR_HOST`, mounted at
   `/workspace`) and nothing else of the host filesystem. Not `$HOME`,
   not `/`, and specifically not the Docker socket — mounting that in
   would let a command inside the sandbox control Docker on the host,
   which is a complete escape. brokkr never does this.
2. **No network by default.** `--network none` unless explicitly
   overridden. A command that tries to exfiltrate data or download
   something has nowhere to send or fetch it.
3. **Enforced resource limits.** CPU, memory, and PID limits are always
   applied (`BROKKR_SANDBOX_*_LIMIT` in `.env.example`) — the PID limit
   in particular is a second, independent layer against fork bombs, on
   top of the policy blocklist's pattern match for the same thing.
4. **A non-privileged process.** `--cap-drop ALL`, `--security-opt
   no-new-privileges`, never `--privileged`. The container runs as the
   host user's own uid/gid (not root, and not a synthetic
   container-only uid — see the Stage 1 note in CHANGELOG.md for why
   that specific choice was necessary for the mount to even be usable).

Given these four properties: a command that goes catastrophically wrong
inside the sandbox can, at worst, destroy the contents of
`~/brokkr-workspace` (or whatever `BROKKR_SANDBOX_WORKDIR_HOST` points
at) — because that's real, writable storage, not a decoy — and consume
resources up to the configured limits. It cannot read or modify anything
else on the host, cannot reach the network, and cannot escalate
privileges inside the container.

## What was actually tested

Not just designed — manually run, once per property, during Stage 1
development (see CHANGELOG.md's Stage 1 entry for the full detail):

- **`rm -rf /` inside the container.** Confirmed everything outside the
  bind-mounted workspace was untouched on the host afterward; confirmed
  the workspace's own contents *were* deleted, which is correct — it's a
  real writable mount, not a sealed area.
- **An infinite loop with a short timeout.** Confirmed the process was
  actually killed (not just reported as timed out) by inspecting the
  container's process list afterward, and confirmed no runaway process
  was left consuming resources.
- **A memory allocation beyond the cgroup limit.** `docker inspect` and
  the container's cgroup files both reported a 2 GiB RAM limit. Docker's
  default configuration also allowed 2 GiB of swap, so a touched 3 GiB
  allocation completed through swap; this is why a test must exceed the
  combined 4 GiB ceiling rather than assume 2 GiB is a total-allocation
  limit. A Python process that attempted a 5 GiB allocation while
  touching every page was killed with exit 137, and `memory.events`
  recorded the OOM kill. The
  container itself remained running with only its init process and
  accepted a normal follow-up command.
- **A legitimate-looking runaway process loop against the PID limit.**
  With both Docker and the container cgroup reporting a limit of 256, a
  Python loop attempting to start 400 sleeping child processes stopped
  at 253 children with `EAGAIN` while `pids.current` was exactly 256.
  The cgroup's limit-hit counter increased, every child was then reaped,
  the container returned to its single init process, and a follow-up
  command succeeded.
- **Symlinks pointing outside `/workspace`.** A symlink from the mounted
  workspace to `/etc` resolved inside the container's filesystem
  namespace: a file read through the link had the same hash as the
  container's direct `/etc` path and a different hash from the host's
  corresponding file. A write through the link failed with permission
  denied under the sandbox user and created no file in either the
  container or host `/etc`.
- **A network request to an external host with `--network none`.**
  Confirmed it failed to connect, not just failed slowly.
- **A root-only package installation as the sandbox user.** `apt-get
  install` exited 100 immediately with an explicit permission-denied
  error for the dpkg frontend lock and asked whether the caller was root.
  No package-manager process remained, and the container accepted a
  follow-up command normally.
- **A non-zero exit code and normal successful output**, to confirm
  those ordinary cases are reported accurately and not silently
  swallowed or misreported as something else.
- **Adversarial task descriptions and explicit memory notes.** Fake system
  and developer messages, claims that destructive commands were already
  approved, and persistent memory instructions were sent through the real
  proposal flow. The model could be induced to propose `rm -rf /workspace`,
  `mkfs`, raw-device `dd`, and a recognizable fork bomb. Every proposal still
  required a human decision; direct blocklisted forms were recorded as blocked
  after approval and created no execution row. Closing stdin at the approval
  prompt aborted rather than implicitly approving. Two malicious memory notes
  did not override unrelated tasks in these tests, but that model behavior is
  not relied on as a security property.
- **Malformed adversarial output.** A prompt that induced an empty argv was
  rejected cleanly by the deterministic proposal validator before approval or
  execution. A requested bare pipe was instead repaired by the model into an
  explicit `bash -c` command and still required human review.
- **A deliberate container-escape attempt pass**, run against a disposable
  VM specifically so it could be aggressive: `mount` inside the container
  failed ("must be superuser"); creating a raw socket (`CAP_NET_RAW`) raised
  `PermissionError`; writing to `/proc/sys/kernel/sysrq` failed with
  "Read-only file system"; `/proc/1/root/` resolved to the container's own
  overlay filesystem (confirmed via the `.dockerenv` marker), not the host's;
  `/dev` contained only the standard minimal Docker device set (`null`,
  `zero`, `random`, `urandom`, `tty`, `full`) with no raw host block device
  present, and both a direct read of the VM's own disk device and `mknod`
  attempting to create one failed cleanly. A thread-based PID-exhaustion
  attempt (2,000 threads, not the process-fork shape already covered above)
  stopped at the same `pids.current=256` cgroup limit, confirming the limit
  holds regardless of thread vs. process technique; the container returned
  to a clean two-process state afterward with no leftover threads. `ps aux`
  inside the container showed only the container's own processes (PID
  namespace isolation), never the host's ~180 real processes. Host-side
  network monitoring (kernel/ufw logs) during the entire pass recorded
  nothing unexpected. No attempt reached outside the container.

If you find a way past any of these four properties, that's a real
security bug — see "Reporting a vulnerability" below, not a public
issue.

## What this deliberately does not defend against

- **A kernel exploit.** Docker containers share the host kernel. If the
  model (or someone feeding it adversarial input) finds and uses a
  container-escape kernel vulnerability, this project's sandbox does not
  stop it. gVisor or Firecracker-based isolation would meaningfully
  raise this bar; brokkr does not currently use either, by choice — see
  README.md's "What this is not."
- **A model that convinces its human operator to approve something bad.**
  Human review (step [2] in docs/architecture.md) is a real gate, but it
  is exactly as good as the human's judgment in the moment. brokkr
  cannot force a careful review; it can only make the reviewed
  information (reasoning, exact argv) as clear as possible before asking.
- **Multi-tenant isolation.** There is no user model. Anyone who can run
  the `brokkr` CLI has the same access as anyone else who can. This is a
  single-operator tool.
- **Resource exhaustion from a legitimately-approved command.** The
  resource limits stop runaway/accidental exhaustion; they don't stop a
  human from approving something that's simply expensive to run.

## Defense-in-depth layers, and why they exist despite not being the real boundary

- **`permissions/policy.py`'s static blocklist** exists to give a fast,
  auditable "never executed" rejection for a short list of known
  catastrophic patterns (`rm -rf /`, `dd` to a raw block device, `mkfs`,
  recognizable fork bombs), and to avoid needlessly destroying and
  rebuilding the sandbox rootfs for commands that were never going
  anywhere legitimate. It is explicitly *not* a general shell-attack
  filter — brokkr never uses `shell=True` anywhere (argv lists only,
  matching the discipline in `sandbox/docker_sandbox.py`), so classic
  shell metacharacter attacks (`;`, `&&`, `|`, `>`) simply don't work as
  shell syntax when passed as argv elements in the first place.
- **The remembered-approval store is exact-match only.** See
  docs/architecture.md's "Approval matching" section for the full
  reasoning — in short, "similar" commands can have wildly different
  consequences, so similarity is never used as a basis for skipping
  human review.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
