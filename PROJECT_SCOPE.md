# Secure Local RAG — Project Scope

## Version 1: agreed scope

### Goal
Build a single-user, locally operated RAG assistant that indexes a shared Google Drive folder and answers questions from its Google Docs using local inference.

### Users and access
- One employee uses the system on the same Windows computer that hosts it.
- All documents in the configured Drive folder tree are in scope; there are no document-level or user-specific permission rules in v1.
- The AnythingLLM Docker instance remains bound to `127.0.0.1` so it is not reachable from other computers by default.
- Keep AnythingLLM in single-user mode and enable instance password protection.
- Do not enable multi-user mode or LAN access as part of v1.

### Data sources
- Google Docs only.
- One configured Google Drive folder, including nested subfolders.
- Google Docs are exported to PDF for ingestion.
- Google Sheets, Slides, uploaded PDFs, DOCX, and other file types are explicitly out of scope for v1.

### Processing and storage
- Google Drive API is used with read-only access for synchronization.
- Python Sync Agent checks the folder every 15 minutes by default.
- SQLite stores the synchronization manifest and document state.
- Unchanged documents should not be downloaded or re-indexed unnecessarily.
- AnythingLLM manages document parsing, chunking, workspace retrieval, and LanceDB.
- Ollama runs the chat and embedding models locally.
- Llama 3 8B is the initial chat model; `nomic-embed-text` is the initial embedding model.
- The target inference hardware is an NVIDIA RTX 3070 with 8 GB VRAM; actual latency and memory use must be measured.

### Privacy boundary
- Document parsing, embeddings, vector storage, and LLM inference are intended to remain on the local computer.
- Google Drive remains a cloud source. The sync agent must connect to Google's API over the internet to retrieve source documents and metadata.
- Do not describe the whole solution as fully air-gapped or 100% on-premise. A more accurate description is: **local/on-premise AI processing and inference, with outbound Google Drive synchronization**.
- Do not configure a hosted LLM or hosted embedding provider.

## Explicitly deferred (not v1 requirements)

1. Multiple employees connecting to one shared server.
2. A dedicated always-on company computer or mini workstation.
3. LAN exposure, reverse proxy, TLS certificates, and remote access controls.
4. Per-user or per-document authorization rules.
5. PDF, Word, and Google Sheets ingestion.
6. Event-driven Drive notifications or Google Drive Changes API optimization.
7. Reranking, separate databases, queues, or other infrastructure services.

## Future expansion path

If the single-user pilot proves useful:

1. Move the Docker deployment and persistent storage to a company-owned, always-on host.
2. Select hardware based on measured model latency, concurrent usage, RAM, storage, and GPU VRAM. A typical small office mini PC may not be sufficient for local LLM inference; a compact workstation or a mini PC with a suitable external/attached GPU may be needed.
3. Enable AnythingLLM multi-user mode deliberately, configure user accounts and workspace access, then expose the UI only on the company LAN behind suitable network controls.
4. Add PDF and Sheets support through separate source-specific exporters/parsers while keeping the existing sync-state and ingestion workflow.
5. Add finer document permissions only if the company needs them.

## v1 acceptance checks

- A new Google Doc is indexed once.
- An unchanged document is skipped on the next scan.
- An edited document replaces the old indexed content without leaving duplicate active copies in the workspace.
- A deleted or out-of-scope document is no longer retrievable from the workspace.
- A failed upload or embedding operation is logged and retried safely.
- Restarting the agent does not reset its synchronization history.
- AnythingLLM is not reachable from another computer in v1.
- Questions are answered using the configured local Ollama models, with source references visible where supported.

## Important deletion distinction

Removing a document from a workspace is not necessarily the same as permanently deleting its parsed source and cached vectors from AnythingLLM's global document library. The current sync agent removes stale documents from the workspace. Before calling the process a complete source-to-target mirror, verify whether deleted Drive documents should also be purged from AnythingLLM's global document library and confirm the exact supported API operation for the installed AnythingLLM version.
