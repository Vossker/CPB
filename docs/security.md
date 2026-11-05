# Security model and limitations

## Implemented controls

- User API keys, worker tokens, and per-run lease tokens are distinct. Persisted credentials are
  HMAC-SHA256 hashes keyed by `CPB_API_SECRET`; responses never expose hashes or storage secrets.
- Owner filters are part of every publisher task/log/artifact query. A worker token resolves one
  node, and a lease token can update only the run assigned to that node.
- Runtime images are globally allow-listed. CPU, memory, timeout, retry, bundle, and artifact
  limits are validated before state changes.
- Bundle ZIP extraction rejects absolute paths, `..`, backslashes, symlinks, and expansion beyond
  the disk allowance. Artifact paths must be normalized relative paths and regular files inside
  the workspace.
- The server treats worker metadata as untrusted and compares it with S3-observed byte length and
  SHA-256 metadata before settlement.
- Docker is invoked with an argument list (`shell=False`), so untrusted values are not interpolated
  into a host shell. Task shell commands run only inside the task container.
- Task containers use `--network none`, `--read-only`, CPU/memory/memory-swap/PID limits,
  `--cap-drop ALL`, `no-new-privileges`, UID/GID `1000:1000`, a bounded `/tmp` tmpfs, and only the
  disposable workspace mount. They are never privileged and receive neither the Docker socket,
  host home directory, nor host credentials.
- Workspaces use OS temporary directories and are removed after every attempt. Worker shutdown
  terminates the child container and records/reports interrupted leases.

## Remaining limitations

Docker shares the host kernel and is not a sufficient boundary for deliberately hostile tenants.
Image vulnerabilities, kernel/container-runtime vulnerabilities, denial-of-service behavior, and
side channels remain possible. Production hostile-code execution should use gVisor, Kata
Containers, Firecracker, or dedicated virtual machines in addition to host hardening.

The portable Docker CLI cannot enforce a hard per-bind-mount quota. CPB limits ZIP expansion and
checks workspace size before and after execution, but a job can temporarily consume more disk.
Production should use a quota-backed filesystem or per-VM volume. PID/memory limits depend on the
host cgroup configuration. UID 1000 ownership also assumes the temporary workspace permits that
container user to write.

The MVP uses bearer API keys rather than MFA/SSO, has no malware scanner, rate limiter, WAF,
egress proxy, signed worker binary, image signature verification, per-tenant encryption key, or
object retention policy. MinIO credentials grant the API broad bucket access. Presigned URLs are
short-lived bearer URLs. Logs and public source must still be treated as untrusted content.

Verification confirms deterministic declared evidence but does not prove semantic correctness
against hidden tests. The `TaskVerifier` interface is intended for trusted validator images or
independent re-execution in a stronger sandbox.

