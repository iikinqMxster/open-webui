"""One-way file sync from Open WebUI chats into per-chat terminal containers.

Files a user attaches to a chat are pushed into the container's ``~/input``
directory (one-way — OWUI → terminal). Generated files are written by the
terminal to ``~/output`` and surfaced back in the chat by *streaming* them
through the terminal proxy — they are never copied into OWUI storage.

The container is scoped per chat via the ``X-Session-Id`` header (the chat id);
uploading a file is what lazily provisions the chat's container, so this module
is also the "ensure the chat has a terminal" path.
"""

import asyncio
import json
import logging
import time

import aiohttp

from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL
from open_webui.models.chats import Chats
from open_webui.models.config import Config
from open_webui.models.files import Files
from open_webui.models.groups import Groups
from open_webui.utils.access_control import has_connection_access

log = logging.getLogger(__name__)

# Directory conventions inside the terminal container home.
INPUT_DIR = "~/input"
OUTPUT_DIR = "~/output"

# Track which chats have provisioned a container this process lifetime, so the
# `create_terminal` tool can be dropped from the model's tool list once the
# chat already has one. TTL'd so a container that has since been reaped lets the
# tool reappear.
_PROVISION_TTL_SECONDS = 900  # 15 minutes


def _provisioned_map(request) -> dict:
    state = request.app.state
    m = getattr(state, "_terminal_provisioned_chats", None)
    if m is None:
        m = {}
        state._terminal_provisioned_chats = m
    return m


def mark_chat_provisioned(request, chat_id: str | None) -> None:
    """Record that *chat_id* has an active terminal container."""
    if request is None or not chat_id:
        return
    try:
        _provisioned_map(request)[chat_id] = time.monotonic()
    except Exception:  # pragma: no cover - defensive
        pass


def chat_has_terminal(request, chat_id: str | None) -> bool:
    """Whether *chat_id* is known to already have a (recent) container."""
    if request is None or not chat_id:
        return False
    try:
        ts = _provisioned_map(request).get(chat_id)
    except Exception:  # pragma: no cover - defensive
        return False
    if ts is None:
        return False
    if time.monotonic() - ts > _PROVISION_TTL_SECONDS:
        return False
    return True


def output_view_url(
    terminal_id: str, path: str = "~/output", chat_id: str | None = None
) -> str:
    """Same-origin proxy URL that streams a file from the container.

    Embeddable directly as a markdown image / link in a chat message — it passes
    ``safeImageUrl`` (leading ``/``) and streams via the terminal proxy without
    copying anything into OWUI storage. ``chat_id`` is passed as the
    ``x_session_id`` query param so the proxy routes to this chat's container
    (an ``<img>`` load can't set the ``X-Session-Id`` header).
    """
    from urllib.parse import quote

    # Keep '/' and '~' readable — they are valid unencoded in a query value and
    # make the URL easy for the model to construct/edit.
    url = f"/api/v1/terminals/{terminal_id}/files/view?path={quote(path, safe='/~._-')}"
    if chat_id:
        url += f"&x_session_id={quote(chat_id, safe='')}"
    return url


async def _get_connection(terminal_id: str) -> dict | None:
    connections = await Config.get("terminal_server.connections", []) or []
    return next((c for c in connections if c.get("id") == terminal_id), None)


def _proxy_base_url(connection: dict) -> str:
    """Base URL for the container, honouring orchestrator policy routing."""
    base = (connection.get("url") or "").rstrip("/")
    policy_id = connection.get("policy_id")
    if policy_id:
        base = f"{base}/p/{policy_id}"
    return base


def _build_headers(
    connection: dict, user, chat_id: str | None, request=None, no_create: bool = False
) -> tuple[dict, dict]:
    """Build auth headers + cookies for a terminal connection.

    Mirrors ``get_terminal_tools`` (utils/tools.py) — bearer / session /
    system_oauth. ``X-Session-Id`` (the chat id) scopes the container per chat.
    When *no_create* is True, sets ``X-Terminal-No-Create`` so the orchestrator
    won't provision a container for this (incidental) request.
    """
    headers: dict = {"X-User-Id": user.id}
    if chat_id:
        headers["X-Session-Id"] = chat_id
    if no_create:
        headers["X-Terminal-No-Create"] = "1"

    cookies: dict = {}
    auth_type = connection.get("auth_type", "bearer")
    key = connection.get("key", "")
    if auth_type == "bearer":
        if key:
            headers["Authorization"] = f"Bearer {key}"
    elif auth_type == "session" and request is not None:
        cookies = dict(request.cookies)
        token = getattr(getattr(request.state, "token", None), "credentials", None)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif auth_type == "system_oauth" and request is not None:
        cookies = dict(request.cookies)
        oauth_token = request.headers.get("x-oauth-access-token", "")
        if oauth_token:
            headers["Authorization"] = f"Bearer {oauth_token}"
    # auth_type == "none": no Authorization header
    return headers, cookies


def _read_file_bytes(file_path: str) -> bytes:
    # Storage.get_file resolves the storage path to a local path (downloading
    # from S3/GCS/Azure if needed); imported lazily to avoid import cycles.
    from open_webui.storage.provider import Storage

    local_path = Storage.get_file(file_path)
    with open(local_path, "rb") as f:
        return f.read()


async def resolve_terminal_connection(user, terminal_id: str) -> dict | None:
    """Return the connection dict if it exists, is enabled, and *user* may use it."""
    connection = await _get_connection(terminal_id)
    if connection is None or not connection.get("enabled", True):
        return None
    if not (connection.get("url") or "").strip():
        return None
    user_group_ids = {g.id for g in await Groups.get_groups_by_member_id(user.id)}
    if not await has_connection_access(user, connection, user_group_ids):
        return None
    return connection


async def ensure_chat_terminal(request, user, chat_id: str, terminal_id: str) -> dict:
    """Provision (or reuse) the chat's container and VERIFY it is actually up.

    Hits ``GET /files/cwd`` (carrying ``X-Session-Id``) which makes the
    orchestrator lazily create the per-chat container; the container is only
    considered ready when it returns HTTP 200 with a valid terminal payload
    (a ``cwd``/``home``). Retries a few times because a freshly provisioned
    container may take a moment to serve. Marks the chat provisioned on success.
    Never raises.

    Returns ``{"ready": bool, "detail": str, "cwd": str | None}`` so callers can
    surface a real reason instead of falsely reporting success.
    """
    if not chat_id or not terminal_id:
        return {"ready": False, "detail": "missing chat id or terminal id"}
    connection = await resolve_terminal_connection(user, terminal_id)
    if connection is None:
        return {
            "ready": False,
            "detail": "terminal connection not found, disabled, or access denied",
        }
    headers, cookies = _build_headers(connection, user, chat_id, request)
    url = f"{_proxy_base_url(connection)}/files/cwd"

    last_detail = "no response from terminal orchestrator"
    attempts = 4
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30, connect=10),
        trust_env=True,
    ) as session:
        for attempt in range(attempts):
            try:
                async with session.get(
                    url, headers=headers, cookies=cookies, ssl=AIOHTTP_CLIENT_SESSION_SSL
                ) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        try:
                            data = json.loads(body)
                        except Exception:
                            data = None
                        # A real Open Terminal /files/cwd returns cwd/home/root.
                        if isinstance(data, dict) and ("cwd" in data or "home" in data):
                            mark_chat_provisioned(request, chat_id)
                            log.info(
                                "terminal ensure: container ready for chat %s (%s) cwd=%s",
                                chat_id, terminal_id, data.get("cwd"),
                            )
                            return {
                                "ready": True,
                                "detail": "ready",
                                "cwd": data.get("cwd"),
                            }
                        last_detail = f"HTTP 200 but unexpected body: {body[:200]}"
                    else:
                        last_detail = f"HTTP {resp.status}: {body[:200]}"
            except Exception as e:
                last_detail = f"request error: {e}"
            if attempt < attempts - 1:
                await asyncio.sleep(1.5)

    log.warning(
        "terminal ensure: container NOT ready for chat %s (%s): %s",
        chat_id, terminal_id, last_detail,
    )
    return {"ready": False, "detail": last_detail}


async def _chat_input_file_ids(chat_id: str) -> list[str]:
    """All input (type=='file') attachments linked to a chat, de-duplicated."""
    try:
        chat_files = await Chats.get_chat_files_by_chat_id(chat_id)
    except Exception as e:  # pragma: no cover - defensive
        log.debug(f"terminal sync: could not list chat files for {chat_id}: {e}")
        return []
    seen: dict[str, None] = {}
    for cf in chat_files:
        if cf.file_id:
            seen.setdefault(cf.file_id, None)
    return list(seen.keys())


async def terminal_container_enabled() -> bool:
    """Whether the terminal-container feature (tool + auto-sync) is enabled."""
    try:
        return bool(await Config.get("terminal_container.enable", True))
    except Exception:
        return True


async def schedule_chat_file_sync(
    request,
    user,
    chat_id: str | None,
    terminal_id: str | None,
    file_ids: list[str] | None = None,
    event_emitter=None,
) -> bool:
    """Fire-and-forget one-way sync (used from the chat-completion hot path).

    Returns True if a sync task was scheduled. Never blocks on the upload and
    never raises — file sync must not break chat completion. When *event_emitter*
    is given, emits a ``terminal:sync`` event after a successful sync so the file
    panel refreshes.
    """
    if not chat_id:
        log.info("terminal sync: skipped — no chat_id")
        return False
    if not terminal_id:
        log.info(
            "terminal sync: skipped for chat %s — no terminal_id on the request "
            "(is a terminal selected and terminal capability enabled for the model?)",
            chat_id,
        )
        return False
    if not await terminal_container_enabled():
        log.info("terminal sync: skipped — terminal_container.enable is off")
        return False

    log.info(
        "terminal sync: scheduling for chat=%s terminal=%s (%s)",
        chat_id, terminal_id,
        f"{len(file_ids)} file(s)" if file_ids is not None else "all chat files",
    )

    async def _run() -> None:
        try:
            result = await sync_chat_files_to_terminal(
                request, user, chat_id, terminal_id, file_ids=file_ids
            )
            if event_emitter and (result or {}).get("synced"):
                try:
                    await event_emitter(
                        {"type": "terminal:sync", "data": {"chat_id": chat_id}}
                    )
                except Exception:
                    pass
        except Exception as e:  # pragma: no cover - defensive
            log.warning(f"terminal sync task failed for chat {chat_id}: {e}")

    asyncio.create_task(_run())
    return True


async def sync_chat_files_to_terminal(
    request,
    user,
    chat_id: str,
    terminal_id: str,
    file_ids: list[str] | None = None,
    create: bool = True,
) -> dict:
    """Push chat input files into the chat's container ``~/input`` (one-way).

    - ``file_ids=None`` syncs every input file linked to the chat (used on first
      open / the built-in tool).
    - ``file_ids=[...]`` syncs just those (used as the delta when new files are
      attached).

    Uploading provisions the per-chat container lazily. Returns a small summary
    dict; never raises — sync failures must not break chat completion.
    """
    if not chat_id or not terminal_id:
        return {"synced": 0, "skipped": 0, "error": "missing_chat_or_terminal"}

    connection = await resolve_terminal_connection(user, terminal_id)
    if connection is None:
        log.info(
            "terminal sync: terminal %s unavailable/denied for user %s (chat %s)",
            terminal_id, getattr(user, "id", "?"), chat_id,
        )
        return {"synced": 0, "skipped": 0, "error": "terminal_unavailable"}

    if file_ids is None:
        file_ids = await _chat_input_file_ids(chat_id)
    else:
        # de-dup, preserve order, drop falsy ids
        file_ids = list(dict.fromkeys([fid for fid in file_ids if fid]))
    if not file_ids:
        log.info("terminal sync: chat %s has no input files to sync", chat_id)
        return {"synced": 0, "skipped": 0}

    files = await Files.get_files_by_ids(file_ids)
    headers, cookies = _build_headers(
        connection, user, chat_id, request, no_create=not create
    )
    upload_url = f"{_proxy_base_url(connection)}/files/upload"
    log.info(
        "terminal sync: uploading %d file(s) to %s (%s) for chat %s",
        len(files), upload_url, INPUT_DIR, chat_id,
    )

    synced = 0
    skipped = 0
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120, connect=10),
        trust_env=True,
    ) as session:
        for file in files:
            try:
                data = await asyncio.to_thread(_read_file_bytes, file.path)
            except Exception as e:
                log.warning(f"terminal sync: cannot read file {file.id}: {e}")
                skipped += 1
                continue

            content_type = (file.meta or {}).get("content_type")
            if not isinstance(content_type, str) or not content_type:
                content_type = "application/octet-stream"
            form = aiohttp.FormData()
            form.add_field(
                "file",
                data,
                filename=file.filename or file.id,
                content_type=content_type,
            )
            try:
                async with session.post(
                    upload_url,
                    params={"directory": INPUT_DIR},
                    data=form,
                    headers=headers,
                    cookies=cookies,
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                ) as resp:
                    if resp.status < 400:
                        synced += 1
                    else:
                        skipped += 1
                        body = (await resp.text())[:200]
                        log.warning(
                            f"terminal sync: upload of {file.filename} failed "
                            f"({resp.status}): {body}"
                        )
            except Exception as e:
                skipped += 1
                log.warning(f"terminal sync: upload error for {file.filename}: {e}")

    if synced:
        # A successful upload means the chat's container is provisioned.
        mark_chat_provisioned(request, chat_id)

    log.info(
        f"terminal sync: chat={chat_id} terminal={terminal_id} "
        f"synced={synced} skipped={skipped} -> {INPUT_DIR}"
    )
    return {
        "synced": synced,
        "skipped": skipped,
        "input_dir": INPUT_DIR,
        "output_dir": OUTPUT_DIR,
    }
