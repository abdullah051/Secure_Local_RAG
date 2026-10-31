# Gemini Review Request — Secure Local RAG v1

Act as an independent senior AI infrastructure engineer and security-minded Python reviewer. Review the attached project files and architecture critically. Do not redesign the system from scratch and do not add infrastructure merely for sophistication.

## Project goal
A single employee uses a locally hosted RAG assistant on one Windows computer. The assistant synchronizes one shared Google Drive folder (including nested folders), indexes Google Docs only, and answers questions using local Ollama models. All documents in the folder are visible to the sole user; there are no per-user or per-document permissions in v1.

## Agreed v1 constraints
- One user on the same machine.
- Google Docs only; export each Google Doc to PDF for ingestion.
- One configured Drive folder tree, including nested folders.
- Google Drive API access is read-only and outbound; Google Drive remains a cloud dependency.
- Python Sync Agent runs every 15 minutes by default.
- SQLite is acceptable for sync state.
- AnythingLLM Docker is used for its local UI, workspace, API, and LanceDB integration.
- Ollama runs locally on an NVIDIA RTX 3070 8 GB GPU.
- Initial chat model: Llama 3 8B.
- Initial embedding model: nomic-embed-text.
- Keep the AnythingLLM port bound to localhost for v1; do not expose the service to the LAN.
- Keep the architecture small. No Kubernetes, Kafka, Redis, or external database unless you can demonstrate a concrete requirement.

## Future possibilities, not current requirements
- Several employees may later connect to one always-on company host.
- The company may later purchase a dedicated compact workstation/server.
- PDF and Google Sheets support may be added later.
- There are currently no different document permissions.

## Review tasks

1. **Architecture fit:** Is the chosen set of components appropriate for this scope? Identify only changes that solve a real requirement.
2. **Python code audit:** Inspect the complete sync agent for logic errors, race conditions, missing pagination, path/name collisions, retry hazards, state-transition problems, duplicate ingestion, and unsafe deletion behavior.
3. **Google Drive correctness:** Verify service-account access assumptions, folder traversal, shared-drive handling, export behavior, modified-time checks, and deletion/move detection against current Google Drive API documentation.
4. **AnythingLLM API compatibility:** Verify every endpoint, request payload, response field, and document identifier used by the script against the current official API documentation or the installed instance's Swagger specification. Do not assume an endpoint exists. Clearly mark anything that must be confirmed at `/api/docs` on the actual installation.
5. **Deletion semantics:** Distinguish removing a document from the active workspace from permanently deleting its parsed file and cached embeddings from AnythingLLM's global document library. Recommend the least risky v1 behavior and state whether the current code actually achieves it.
6. **Security:** Review service-account scope, secret storage, API-key handling, Docker port binding, AnythingLLM single-user password protection, telemetry/network egress, and the use of Docker capabilities. Do not claim the system is air-gapped: Google Drive synchronization requires internet access.
7. **Local inference:** Assess whether RTX 3070 8 GB is a plausible starting point for Llama 3 8B plus nomic-embed-text; identify what must be measured rather than guessing.
8. **Future expansion:** Explain the smallest migration path from one local user to multiple employees using one always-on host, without implementing multi-user or LAN access now.
9. **Test plan:** Provide a short practical test matrix for new, unchanged, edited, deleted, moved, inaccessible, and failed documents, plus restart/recovery behavior.

## Required response format

- Start with an overall verdict: suitable / suitable with fixes / not suitable.
- List issues by severity: Critical, High, Medium, Low.
- For each issue provide: exact file/function/setting, why it matters, minimal recommended fix, and evidence/source link.
- Separate confirmed defects from assumptions and version-dependent API details.
- Prioritize the smallest changes that make v1 reliable. Avoid speculative refactors and over-engineering.
- Do not rewrite the entire codebase unless a specific defect makes that necessary. If proposing code changes, show focused patches and explain their effect.
- Identify any features or claims in the README that overstate the implementation.

## Security instruction
The project archive should contain placeholders only. Do not ask for, expose, or reproduce real Google credentials, `.env` values, AnythingLLM API keys, passwords, or private company documents. Treat these as secrets and keep them local.
