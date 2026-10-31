"""Google Drive -> AnythingLLM local document synchronizer.

Scope of this first production-minded version:
- Google Docs files only (exported to PDF)
- One configured Drive folder, including nested folders
- Metadata scan every N minutes; download/embed only changed content
- SQLite state, retries for transient API failures, deletion handling, logging

The script does not send document content to a hosted LLM. Google Drive remains
an external cloud source and is contacted over HTTPS for synchronization.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ----------------------------- Configuration -----------------------------
_credentials_path = Path(
    os.getenv("GOOGLE_CREDENTIALS_FILE", str(BASE_DIR / "credentials.json"))
)
GOOGLE_CREDENTIALS_FILE = (
    _credentials_path if _credentials_path.is_absolute() else (BASE_DIR / _credentials_path).resolve()
)
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "").strip()

ANYTHINGLLM_BASE_URL = os.getenv(
    "ANYTHINGLLM_BASE_URL", "http://127.0.0.1:3001/api/v1"
).rstrip("/")
ANYTHINGLLM_API_KEY = os.getenv("ANYTHINGLLM_API_KEY", "").strip()
WORKSPACE_SLUG = os.getenv("WORKSPACE_SLUG", "").strip()

CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "15"))
def project_path_from_env(name: str, default: Path) -> Path:
    raw = Path(os.getenv(name, str(default)))
    return raw if raw.is_absolute() else (BASE_DIR / raw).resolve()


DB_PATH = project_path_from_env("STATE_DB_PATH", BASE_DIR / "data" / "sync_state.sqlite3")
LOG_PATH = project_path_from_env("LOG_PATH", BASE_DIR / "logs" / "sync_agent.log")
TEMP_DIR = project_path_from_env("TEMP_DIR", BASE_DIR / "tmp")

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_READONLY_SCOPE = ["https://www.googleapis.com/auth/drive.readonly"]
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
# -------------------------------------------------------------------------


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )


def validate_config() -> None:
    missing = []
    if not DRIVE_FOLDER_ID:
        missing.append("DRIVE_FOLDER_ID")
    if not ANYTHINGLLM_API_KEY:
        missing.append("ANYTHINGLLM_API_KEY")
    if not WORKSPACE_SLUG:
        missing.append("WORKSPACE_SLUG")
    if missing:
        raise RuntimeError("Missing .env settings: " + ", ".join(missing))
    if not GOOGLE_CREDENTIALS_FILE.is_file():
        raise FileNotFoundError(
            f"Google credentials file not found: {GOOGLE_CREDENTIALS_FILE}"
        )
    if CHECK_INTERVAL_MINUTES < 1:
        raise ValueError("CHECK_INTERVAL_MINUTES must be at least 1")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------ SQLite state ------------------------------
class SyncDatabase:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def connection(self):
        # sqlite3's connection context manager commits/rolls back but does not
        # close the connection, so close it explicitly for this long-running daemon.
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    file_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    modified_time TEXT,
                    sha256 TEXT,
                    anythingllm_location TEXT,
                    pending_location TEXT,
                    pending_sha256 TEXT,
                    pending_modified_time TEXT,
                    pending_name TEXT,
                    status TEXT NOT NULL DEFAULT 'NEW',
                    last_error TEXT,
                    last_synced_at TEXT
                )
                """
            )

    def get_document(self, file_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE file_id = ?", (file_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_documents(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM documents").fetchall()
            return [dict(row) for row in rows]

    def mark_seen(self, item: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO documents (file_id, name, mime_type, modified_time, status)
                VALUES (?, ?, ?, ?, 'NEW')
                ON CONFLICT(file_id) DO UPDATE SET
                    name = excluded.name,
                    mime_type = excluded.mime_type
                """,
                (item["id"], item.get("name", "Untitled"), item["mimeType"], item.get("modifiedTime")),
            )

    def save_error(self, item: dict[str, Any], error: Exception) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO documents (file_id, name, mime_type, modified_time, status, last_error)
                VALUES (?, ?, ?, ?, 'FAILED', ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    name = excluded.name,
                    mime_type = excluded.mime_type,
                    last_error = excluded.last_error,
                    status = CASE
                        WHEN documents.pending_location IS NOT NULL THEN 'PENDING_EMBED'
                        ELSE 'FAILED'
                    END
                """,
                (
                    item["id"],
                    item.get("name", "Untitled"),
                    item.get("mimeType", GOOGLE_DOC_MIME),
                    item.get("modifiedTime"),
                    str(error)[:2000],
                ),
            )

    def save_pending(
        self,
        file_id: str,
        name: str,
        mime_type: str,
        modified_time: str | None,
        sha256: str,
        location: str,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE documents SET
                    name = ?, mime_type = ?, pending_location = ?,
                    pending_sha256 = ?, pending_modified_time = ?, pending_name = ?,
                    status = 'PENDING_EMBED', last_error = NULL
                WHERE file_id = ?
                """,
                (name, mime_type, location, sha256, modified_time, name, file_id),
            )

    def finalize_pending(self, file_id: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE documents SET
                    anythingllm_location = pending_location,
                    sha256 = pending_sha256,
                    modified_time = pending_modified_time,
                    name = COALESCE(pending_name, name),
                    pending_location = NULL,
                    pending_sha256 = NULL,
                    pending_modified_time = NULL,
                    pending_name = NULL,
                    status = 'SYNCED',
                    last_error = NULL,
                    last_synced_at = ?
                WHERE file_id = ?
                """,
                (utc_now(), file_id),
            )

    def update_unchanged(self, file_id: str, item: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE documents SET name = ?, mime_type = ?, modified_time = ?,
                    status = 'SYNCED', last_error = NULL
                WHERE file_id = ?
                """,
                (item.get("name", "Untitled"), item["mimeType"], item.get("modifiedTime"), file_id),
            )

    def delete_document_record(self, file_id: str) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM documents WHERE file_id = ?", (file_id,))


# ---------------------------- Google Drive API ----------------------------
def get_drive_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials = service_account.Credentials.from_service_account_file(
        str(GOOGLE_CREDENTIALS_FILE), scopes=DRIVE_READONLY_SCOPE
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def list_folder_children(service, folder_id: str) -> list[dict[str, Any]]:
    """List all direct children, following Drive API pagination."""
    items: list[dict[str, Any]] = []
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                pageSize=1000,
                pageToken=page_token,
                spaces="drive",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                fields=(
                    "nextPageToken, files(id,name,mimeType,modifiedTime,webViewLink,parents)"
                ),
            )
            .execute(num_retries=3)
        )
        items.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return items


def scan_configured_folder(service) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Return Google Docs and all reachable item IDs under the configured folder.

    The traversal is recursive. If any folder listing fails, this function raises;
    deletion reconciliation is therefore skipped for that cycle.
    """
    # Validate the configured root before interpreting an empty listing as
    # deletion. If the service account loses access to the root folder, fail
    # the cycle and preserve the existing index.
    root = (
        service.files()
        .get(
            fileId=DRIVE_FOLDER_ID,
            supportsAllDrives=True,
            fields="id,mimeType,trashed",
        )
        .execute(num_retries=3)
    )
    if root.get("mimeType") != GOOGLE_FOLDER_MIME or root.get("trashed"):
        raise RuntimeError("Configured DRIVE_FOLDER_ID is not an accessible, active folder")

    queue = deque([DRIVE_FOLDER_ID])
    visited_folders: set[str] = set()
    all_ids: set[str] = set()
    google_docs: dict[str, dict[str, Any]] = {}

    while queue:
        folder_id = queue.popleft()
        if folder_id in visited_folders:
            continue
        visited_folders.add(folder_id)

        for item in list_folder_children(service, folder_id):
            file_id = item["id"]
            all_ids.add(file_id)
            mime_type = item.get("mimeType", "")
            if mime_type == GOOGLE_FOLDER_MIME:
                queue.append(file_id)
            elif mime_type == GOOGLE_DOC_MIME:
                google_docs[file_id] = item

    return google_docs, all_ids


def export_google_doc_to_pdf(service, file_id: str, destination: Path) -> None:
    from googleapiclient.http import MediaIoBaseDownload

    request = service.files().export_media(fileId=file_id, mimeType="application/pdf")
    with destination.open("wb") as output:
        downloader = MediaIoBaseDownload(output, request, chunksize=1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=3)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for block in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ----------------------------- AnythingLLM API ----------------------------
def api_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ANYTHINGLLM_API_KEY}"}


def request_with_retry(method: str, url: str, *, json_body: dict | None = None) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = requests.request(
                method,
                url,
                headers={**api_headers(), "Content-Type": "application/json"},
                json=json_body,
                timeout=(10, 180),
            )
            if response.status_code in TRANSIENT_HTTP_STATUSES and attempt < 3:
                logging.warning("Transient AnythingLLM HTTP %s; retry %s/3", response.status_code, attempt)
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 3:
                break
            logging.warning("AnythingLLM request failed; retry %s/3: %s", attempt, exc)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"AnythingLLM request failed after retries: {last_error}")


def verify_anythingllm() -> None:
    response = requests.get(
        f"{ANYTHINGLLM_BASE_URL}/auth",
        headers=api_headers(),
        timeout=(10, 30),
    )
    response.raise_for_status()
    auth_result = response.json()
    if auth_result.get("authenticated") is not True:
        raise RuntimeError("AnythingLLM API key was not accepted")


def upload_pdf_to_anythingllm(pdf_path: Path, item: dict[str, Any]) -> str:
    """Upload a PDF to AnythingLLM's document collector and return its location."""
    url = f"{ANYTHINGLLM_BASE_URL}/document/upload"
    # A title and Drive link help the document remain identifiable in the UI.
    metadata = {
        "title": item.get("name", "Untitled"),
        "description": f"Synchronized from Google Drive. Drive file ID: {item['id']}",
        "docSource": item.get("webViewLink", "Google Drive"),
    }

    # Do not retry an ambiguous upload timeout automatically: the server may have
    # received the file even if the client did not receive its response.
    with pdf_path.open("rb") as file_obj:
        response = requests.post(
            url,
            headers=api_headers(),
            files={"file": (f"{item['id']}.pdf", file_obj, "application/pdf")},
            data={"metadata": json.dumps(metadata)},
            timeout=(10, 300),
        )
    response.raise_for_status()
    payload = response.json()
    documents = payload.get("documents") or []
    if payload.get("success") is False or not documents or not documents[0].get("location"):
        raise RuntimeError(f"Unexpected AnythingLLM upload response: {payload}")
    return documents[0]["location"]


def update_workspace_embeddings(adds: list[str], deletes: list[str]) -> None:
    slug = quote(WORKSPACE_SLUG, safe="")
    url = f"{ANYTHINGLLM_BASE_URL}/workspace/{slug}/update-embeddings"
    payload = {"adds": adds, "deletes": deletes}
    response = request_with_retry("POST", url, json_body=payload)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if body.get("success") is False:
        raise RuntimeError(f"Workspace embedding update failed: {body}")


# ---------------------------- Sync orchestration --------------------------
def complete_pending_embedding(db: SyncDatabase, record: dict[str, Any]) -> None:
    pending_location = record.get("pending_location")
    if not pending_location:
        return

    old_location = record.get("anythingllm_location")
    deletes = [old_location] if old_location and old_location != pending_location else []
    logging.info("Embedding pending document update: %s", record["name"])
    update_workspace_embeddings([pending_location], deletes)
    db.finalize_pending(record["file_id"])
    logging.info("Synced: %s", record["name"])


def process_google_doc(service, db: SyncDatabase, item: dict[str, Any]) -> None:
    file_id = item["id"]
    db.mark_seen(item)
    record = db.get_document(file_id)

    # Resume a prior upload if the process stopped after upload but before indexing.
    if record and record.get("pending_location"):
        complete_pending_embedding(db, record)
        record = db.get_document(file_id)

    modified_time = item.get("modifiedTime")
    if (
        record
        and record.get("status") == "SYNCED"
        and record.get("modified_time") == modified_time
        and record.get("anythingllm_location")
    ):
        logging.debug("Unchanged; skipping download: %s", item.get("name"))
        return

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".pdf", prefix=f"{file_id}_", dir=TEMP_DIR, delete=False
        ) as temp_file:
            temp_path = Path(temp_file.name)

        logging.info("Exporting changed Google Doc: %s", item.get("name"))
        export_google_doc_to_pdf(service, file_id, temp_path)
        content_hash = sha256_file(temp_path)

        # modifiedTime is the fast change detector; SHA-256 prevents needless
        # re-embedding if an export is byte-identical despite metadata changes.
        if record and record.get("sha256") == content_hash and record.get("anythingllm_location"):
            db.update_unchanged(file_id, item)
            logging.info("Content unchanged; refreshed metadata only: %s", item.get("name"))
            return

        new_location = upload_pdf_to_anythingllm(temp_path, item)
        db.save_pending(
            file_id=file_id,
            name=item.get("name", "Untitled"),
            mime_type=item["mimeType"],
            modified_time=modified_time,
            sha256=content_hash,
            location=new_location,
        )
        record = db.get_document(file_id)
        if record:
            complete_pending_embedding(db, record)
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                logging.warning("Could not remove temporary file: %s", temp_path)


def remove_from_workspace(db: SyncDatabase, record: dict[str, Any]) -> None:
    locations = []
    for key in ("anythingllm_location", "pending_location"):
        location = record.get(key)
        if location and location not in locations:
            locations.append(location)

    if locations:
        logging.info("Removing deleted/out-of-scope document: %s", record["name"])
        update_workspace_embeddings([], locations)
    db.delete_document_record(record["file_id"])
    logging.info("Removed sync state: %s", record["name"])


def sync_once(db: SyncDatabase) -> None:
    logging.info("Starting Drive synchronization")
    verify_anythingllm()
    drive_service = get_drive_service()

    # Do not perform deletion reconciliation unless the entire recursive scan
    # succeeded. A partial scan must never be interpreted as mass deletion.
    google_docs, _all_ids = scan_configured_folder(drive_service)
    logging.info("Found %d Google Docs in configured folder tree", len(google_docs))

    for item in google_docs.values():
        try:
            process_google_doc(drive_service, db, item)
        except Exception as exc:  # keep processing other files in this cycle
            logging.exception("Failed to sync '%s' (%s)", item.get("name"), item.get("id"))
            db.save_error(item, exc)

    current_doc_ids = set(google_docs.keys())
    for record in db.list_documents():
        if record["file_id"] not in current_doc_ids:
            try:
                remove_from_workspace(db, record)
            except Exception:
                logging.exception("Could not remove stale document '%s'", record.get("name"))

    logging.info("Drive synchronization cycle completed")


def main() -> None:
    configure_logging()
    validate_config()
    db = SyncDatabase(DB_PATH)
    logging.info("Starting Google Drive -> AnythingLLM sync agent")
    logging.info("Workspace: %s | Interval: %d minutes", WORKSPACE_SLUG, CHECK_INTERVAL_MINUTES)

    while True:
        started = time.monotonic()
        try:
            sync_once(db)
        except Exception:
            logging.exception("Synchronization cycle failed; state was retained")

        elapsed = time.monotonic() - started
        sleep_for = max(5, CHECK_INTERVAL_MINUTES * 60 - int(elapsed))
        logging.info("Next sync in approximately %d seconds", sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
