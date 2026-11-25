# Security Policy

## Supported Versions

| Version | Supported |
| :--- | :--- |
| `0.3.x` | Yes |
| `< 0.3.0` | No (please upgrade to `0.3.x`) |

---

## Security & Privacy Architecture

PEA is designed to run deterministically on a local workstation with a minimal attack surface:

- **Zero API Cost & Zero Secret Storage:** PEA makes zero outbound LLM API calls and requires no API keys.
- **Loopback-Only Defaults:**
  - `SimRuntimeServer` and `pea-viewer` bind strictly to loopback interfaces (`127.0.0.1`, `localhost`, or `::1`).
  - Both enforce anti-DNS-rebinding `Host` header validation and `X-Content-Type-Options: nosniff`.
  - `pea-viewer` is read-only (rejects non-`GET`/`HEAD` requests), enforces same-origin `Origin` checks, serves strict CSP headers, and makes zero external CDN requests.
- **Sandboxed Simulator Execution:**
  - Local Flash `.swf` simulators run inside pinned Ruffle `0.7.1` with `allowScriptAccess: false` and `allowNetworking: "none"`.
  - Playwright request routing aborts all non-loopback network requests unless an external HTTP(S) origin is explicitly allowlisted via `PEA_ALLOWED_ORIGINS`.
- **Zero Arbitrary Shell or Filesystem Tools:**
  - `pea-mcp` exposes only 18 typed laboratory domain tools plus the read-only `pea://policy` resource.
  - All tool schemas reject unknown fields (`extra="forbid"` / `additionalProperties: false`).
  - All file reads and writes are confined to project/run roots via canonical path resolution, rejecting `..` traversal, NUL bytes, and escaping symlinks.
- **Zero-`eval`/`exec` Numerical & Math Sandbox (`calc_eval`, `data_analyze`, `compute_sim`):**
  - Expressions are parsed and evaluated via an explicit AST allowlist (never `eval()`/`exec()`), blocking attribute/dunder access and enforcing expression length, AST depth, exponent magnitude, ODE `max_nfev`, and wall-clock `timeout_s` caps.
- **Audited `.mcpb` Packaging (`scripts/build_mcpb.py`):**
  - The Claude Desktop `.mcpb` archive is audited before release to guarantee zero path traversal, zero symlinks, zero bundled binaries/SWFs/PDFs/ONNX/WASM payloads, zero literal host home paths, and zero secret patterns.
- **SHA-256 Artifact Integrity Manifest (`provenance.jsonl`):**
  - Every recorded artifact's SHA-256 digest and byte size is checked against disk via `lab_runs(action="verify")` to detect accidental corruption, stale artifacts, or drift during analysis.
  - Because `provenance.jsonl` is stored locally on the user's filesystem without an external signing authority, it provides integrity and reproducibility verification rather than cryptographic tamper-proofing against the machine owner.

---

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report suspected vulnerabilities privately using **GitHub Security Advisories**:

1. Navigate to the repository's **Security** tab on GitHub (`https://github.com/Mottii/PEA/security/advisories`).
2. Click **Report a vulnerability** and provide:
   - Affected PEA version and OS / Python version.
   - A concise description of the security boundary affected (e.g., path confinement, AST sandbox, loopback/Host guard, `.mcpb` launcher, or viewer CSP).
   - Minimal reproduction steps or a proof-of-concept test case.

We aim to acknowledge reports within 72 hours and coordinate a fix and advisory disclosure once verified.
