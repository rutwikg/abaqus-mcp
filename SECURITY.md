# Security Policy

## Supported versions

Fixes land on the latest release. Please upgrade before reporting.

## Reporting a vulnerability

Report privately through GitHub's
[private vulnerability reporting](https://github.com/rutwikg/abaqus-mcp/security/advisories/new)
rather than opening a public issue. Expect an initial response within a week.

## What this software does with your input

Worth understanding before deploying it, because the threat model is unusual for
an MCP server:

- **It executes a solver as a subprocess.** Tools invoke the Abaqus launcher on
  keyword decks and, for model authoring, run Python 2.7 scripts inside
  `abaqus cae`. An Abaqus input deck is not inert data — it can reference
  external files and user subroutines. Treat any deck you did not write as
  untrusted code.
- **It reads and writes the filesystem.** Jobs are staged into
  `ABAQUS_AGENT_RUNS_DIR`, and `read_job_file` returns file contents to the
  client. Point the runs directory somewhere isolated.
- **It consumes licence tokens.** A client that can reach this server can spend
  your Abaqus licences and CPU time.
- **The MCP server has no authentication of its own.** It speaks stdio and
  trusts whatever launches it, so access control is entirely the client's job.
  Do not expose it over a network transport without putting your own
  authentication in front.

## Scope

In scope: anything letting a crafted deck, spec, or tool argument escape the
intended solver invocation — for example command injection through a job name or
path, or a path traversal that reads files outside the runs directory.

Out of scope: vulnerabilities in Abaqus itself (report those to Dassault
Systèmes), and the fact that running an untrusted input deck runs untrusted
code — that is inherent to the solver, and is why the warning above exists.
