import asyncio
import logging
import re
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from open_webui.config import BYPASS_ADMIN_ACCESS_CONTROL
from open_webui.constants import ERROR_MESSAGES
from open_webui.events import EVENTS, publish_event
from open_webui.internal.db import get_async_session
from open_webui.models.access_grants import AccessGrants
from open_webui.models.config import Config
from open_webui.models.groups import Groups
from open_webui.models.models import ModelMeta
from open_webui.models.subagents import (
    SubagentAccessListResponse,
    SubagentAccessResponse,
    SubagentForm,
    SubagentModel,
    SubagentResponse,
    Subagents,
    SubagentUserResponse,
)
from open_webui.retrieval.web.utils import validate_url
from open_webui.utils.access_control import filter_allowed_access_grants, has_permission
from open_webui.utils.auth import get_verified_user
from open_webui.utils.oauth import encrypt_data
from open_webui.utils.remote_agents import enforce_ssrf, fetch_agent_card
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

PAGE_ITEM_COUNT = 30

router = APIRouter()


def _slugify_handle(value: Optional[str]) -> str:
    """Normalize a user-put id / name into a url-safe handle (e.g. 'Math Agent' -> 'math-agent')."""
    value = re.sub(r'[^a-z0-9]+', '-', (value or '').strip().lower())
    return value.strip('-')


def _redact_remote(subagent):
    """Strip remote-agent credentials from a sub-agent before it leaves the server.

    `meta` is returned to everyone with read access, so the stored key must never be
    included; `has_key` tells the editor a credential is set without revealing it.
    """
    try:
        data = subagent.model_dump()
    except AttributeError:
        return subagent

    remote = ((data.get('meta') or {}).get('remote')) or {}
    if isinstance(remote, dict) and remote:
        safe = {key: value for key, value in remote.items() if key not in ('key', 'key_encrypted')}
        safe['has_key'] = bool(remote.get('key_encrypted') or remote.get('key'))
        data['meta'] = {**(data.get('meta') or {}), 'remote': safe}
    return data


async def _prepare_remote_meta(form_data: SubagentForm, user, existing=None) -> None:
    """Validate a remote-agent block and encrypt its credential in place.

    A blank key on update means "keep the existing credential", so an editor that never
    receives the secret can still save other fields without wiping it.
    """
    meta = form_data.meta.model_dump() if form_data.meta else {}
    remote = meta.get('remote')
    if not isinstance(remote, dict) or not remote.get('enabled'):
        return

    url = (remote.get('url') or '').strip()
    if not url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT('A remote agent URL is required'),
        )
    remote['url'] = url

    # Non-admins may only point a remote agent at a publicly routable address, mirroring
    # the SSRF gate applied when the agent is actually invoked.
    if enforce_ssrf(user.role):
        try:
            await asyncio.to_thread(validate_url, url)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT(
                    'This URL is not allowed. Use a publicly reachable https:// address, '
                    'or ask an admin to enable local network access.'
                ),
            )

    key = remote.pop('key', None)
    remote.pop('has_key', None)
    if key:
        remote['key_encrypted'] = encrypt_data(key)
    else:
        previous = ((existing.meta.model_dump() if existing and existing.meta else {}) or {}).get(
            'remote'
        ) or {}
        # `update_subagent_by_id` overwrites the whole meta column, so carry the stored
        # credential forward explicitly or it would be lost on every save.
        if previous.get('key_encrypted'):
            remote['key_encrypted'] = previous['key_encrypted']
        else:
            remote.pop('key_encrypted', None)

    meta['remote'] = remote
    form_data.meta = ModelMeta(**meta)


############################
# GetSubagents
############################


@router.get('/', response_model=list[SubagentUserResponse])
async def get_subagents(
    request: Request,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    if user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL:
        subagents = await Subagents.get_subagents(db=db)
    else:
        user_group_ids = {group.id for group in await Groups.get_groups_by_member_id(user.id, db=db)}
        all_subagents = await Subagents.get_subagents(db=db)
        subagents = [
            subagent
            for subagent in all_subagents
            if subagent.user_id == user.id
            or await AccessGrants.has_access(
                user_id=user.id,
                resource_type='subagent',
                resource_id=subagent.id,
                permission='read',
                user_group_ids=user_group_ids,
                db=db,
            )
        ]

    return [_redact_remote(subagent) for subagent in subagents]


############################
# GetSubagentList
############################


@router.get('/list', response_model=SubagentAccessListResponse)
async def get_subagent_list(
    query: Optional[str] = None,
    view_option: Optional[str] = None,
    page: Optional[int] = 1,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    limit = PAGE_ITEM_COUNT

    page = max(1, page)
    skip = (page - 1) * limit

    filter = {}
    if query:
        filter['query'] = query
    if view_option:
        filter['view_option'] = view_option

    if not (user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL):
        groups = await Groups.get_groups_by_member_id(user.id, db=db)
        if groups:
            filter['group_ids'] = [group.id for group in groups]

        filter['user_id'] = user.id

    result = await Subagents.search_subagents(user.id, filter=filter, skip=skip, limit=limit, db=db)

    return SubagentAccessListResponse(
        items=[
            SubagentAccessResponse(
                **_redact_remote(subagent),
                write_access=(
                    (user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL)
                    or user.id == subagent.user_id
                    or await AccessGrants.has_access(
                        user_id=user.id,
                        resource_type='subagent',
                        resource_id=subagent.id,
                        permission='write',
                        db=db,
                    )
                ),
            )
            for subagent in result.items
        ],
        total=result.total,
    )


############################
# CreateNewSubagent
############################


@router.post('/create', response_model=Optional[SubagentResponse])
async def create_new_subagent(
    request: Request,
    form_data: SubagentForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    if user.role != 'admin' and not await has_permission(
        user.id, 'workspace.subagents', await Config.get('user.permissions'), db=db
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    # The id is an internal, immutable UUID that models bind to (meta.subagentIds). The
    # user-put `handle` (e.g. "math-agent") is the editable, unique identifier the lead agent
    # uses. Because models bind to the UUID, changing the handle never breaks a binding.
    form_data.id = str(uuid4())

    if not (form_data.name or '').strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT('Subagent name is required'),
        )

    # Fall back to a slug of the name (then a short uuid) when the client didn't send a
    # handle, so creation never fails just for lack of a separately-typed id.
    form_data.handle = _slugify_handle(form_data.handle) or _slugify_handle(form_data.name)
    if not form_data.handle:
        form_data.handle = f'subagent-{form_data.id[:8]}'

    existing = await Subagents.get_subagent_by_handle(form_data.handle, db=db)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT('A subagent with this id already exists'),
        )

    await _prepare_remote_meta(form_data, user)

    # Strip public/user grants the requesting user is not permitted to assign.
    form_data.access_grants = await filter_allowed_access_grants(
        await Config.get('user.permissions'),
        user.id,
        user.role,
        form_data.access_grants,
        'sharing.public_subagents',
    )

    try:
        subagent = await Subagents.insert_new_subagent(user.id, form_data, db=db)
        if subagent:
            await publish_event(
                request,
                EVENTS.SUBAGENT_CREATED,
                actor=user,
                subject_id=subagent.id,
                data={'name': subagent.name},
            )
            return _redact_remote(subagent)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT('Error creating subagent'),
            )
    except HTTPException:
        raise
    except Exception as e:
        log.exception(f'Failed to create subagent: {e}')
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e, 'Error creating subagent'),
        )


############################
# GetSubagentById
############################


@router.get('/id/{id}', response_model=Optional[SubagentAccessResponse])
async def get_subagent_by_id(
    id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    subagent = await Subagents.get_subagent_by_id(id, db=db)

    if subagent:
        if (
            user.role == 'admin'
            or subagent.user_id == user.id
            or await AccessGrants.has_access(
                user_id=user.id,
                resource_type='subagent',
                resource_id=subagent.id,
                permission='read',
                db=db,
            )
        ):
            return SubagentAccessResponse(
                **_redact_remote(subagent),
                write_access=(
                    (user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL)
                    or user.id == subagent.user_id
                    or await AccessGrants.has_access(
                        user_id=user.id,
                        resource_type='subagent',
                        resource_id=subagent.id,
                        permission='write',
                        db=db,
                    )
                ),
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# UpdateSubagentById
############################


@router.post('/id/{id}/update', response_model=Optional[SubagentModel])
async def update_subagent_by_id(
    request: Request,
    id: str,
    form_data: SubagentForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    subagent = await Subagents.get_subagent_by_id(id, db=db)
    if not subagent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        subagent.user_id != user.id
        and not await AccessGrants.has_access(
            user_id=user.id,
            resource_type='subagent',
            resource_id=subagent.id,
            permission='write',
            db=db,
        )
        and user.role != 'admin'
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    # The id (UUID) is immutable; the user-put handle may change but is never blanked
    # (fall back to a slug of the name, then the existing handle). Keep handles unique.
    form_data.handle = (
        _slugify_handle(form_data.handle) or _slugify_handle(form_data.name) or subagent.handle
    )
    if form_data.handle and form_data.handle != subagent.handle:
        existing = await Subagents.get_subagent_by_handle(form_data.handle, db=db)
        if existing is not None and existing.id != id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT('A subagent with this id already exists'),
            )

    await _prepare_remote_meta(form_data, user, existing=subagent)

    form_data.access_grants = await filter_allowed_access_grants(
        await Config.get('user.permissions'),
        user.id,
        user.role,
        form_data.access_grants,
        'sharing.public_subagents',
    )

    try:
        updated = {
            **form_data.model_dump(exclude={'id'}),
        }

        subagent = await Subagents.update_subagent_by_id(id, updated, db=db)

        if subagent:
            await publish_event(
                request,
                EVENTS.SUBAGENT_UPDATED,
                actor=user,
                subject_id=subagent.id,
                data={'name': subagent.name},
            )
            return _redact_remote(subagent)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT('Error updating subagent'),
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e, 'Error updating subagent'),
        )


############################
# UpdateSubagentAccessById
############################


class SubagentAccessGrantsForm(BaseModel):
    access_grants: list[dict]


@router.post('/id/{id}/access/update', response_model=Optional[SubagentModel])
async def update_subagent_access_by_id(
    request: Request,
    id: str,
    form_data: SubagentAccessGrantsForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    subagent = await Subagents.get_subagent_by_id(id, db=db)
    if not subagent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        subagent.user_id != user.id
        and not await AccessGrants.has_access(
            user_id=user.id,
            resource_type='subagent',
            resource_id=subagent.id,
            permission='write',
            db=db,
        )
        and user.role != 'admin'
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    form_data.access_grants = await filter_allowed_access_grants(
        await Config.get('user.permissions'),
        user.id,
        user.role,
        form_data.access_grants,
        'sharing.public_subagents',
    )

    await AccessGrants.set_access_grants('subagent', id, form_data.access_grants, db=db)

    subagent = await Subagents.get_subagent_by_id(id, db=db)
    await publish_event(
        request,
        EVENTS.SUBAGENT_UPDATED,
        actor=user,
        subject_id=id,
        data={'access_updated': True, 'name': subagent.name if subagent else None},
    )
    return _redact_remote(subagent)


############################
# ToggleSubagentById
############################


@router.post('/id/{id}/toggle', response_model=Optional[SubagentModel])
async def toggle_subagent_by_id(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    subagent = await Subagents.get_subagent_by_id(id, db=db)
    if subagent:
        if (
            user.role == 'admin'
            or subagent.user_id == user.id
            or await AccessGrants.has_access(
                user_id=user.id,
                resource_type='subagent',
                resource_id=subagent.id,
                permission='write',
                db=db,
            )
        ):
            subagent = await Subagents.toggle_subagent_by_id(id, db=db)

            if subagent:
                await publish_event(
                    request,
                    EVENTS.SUBAGENT_ENABLED if subagent.is_active else EVENTS.SUBAGENT_DISABLED,
                    actor=user,
                    subject_id=subagent.id,
                    data={'name': subagent.name},
                )
                return _redact_remote(subagent)
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ERROR_MESSAGES.DEFAULT('Error toggling subagent'),
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.UNAUTHORIZED,
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# DeleteSubagentById
############################


@router.delete('/id/{id}/delete', response_model=bool)
async def delete_subagent_by_id(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    subagent = await Subagents.get_subagent_by_id(id, db=db)
    if not subagent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        subagent.user_id != user.id
        and not await AccessGrants.has_access(
            user_id=user.id,
            resource_type='subagent',
            resource_id=subagent.id,
            permission='write',
            db=db,
        )
        and user.role != 'admin'
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    result = await Subagents.delete_subagent_by_id(id, db=db)
    if result:
        await publish_event(
            request,
            EVENTS.SUBAGENT_DELETED,
            actor=user,
            subject_id=id,
            data={'name': subagent.name},
        )
    return result


############################
# VerifyRemoteAgent
############################


class RemoteAgentVerifyForm(BaseModel):
    url: str
    auth_type: Optional[str] = 'bearer'
    key: Optional[str] = None
    headers: Optional[dict] = None
    protocol: Optional[str] = 'generic'
    subagent_id: Optional[str] = None


@router.post('/verify')
async def verify_remote_agent(
    request: Request,
    form_data: RemoteAgentVerifyForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Probe a remote agent endpoint: fetch its agent card when one is published.

    Gated by the same permission as creating a sub-agent, and by the same SSRF rule that
    applies when the agent is invoked, so "Test connection" can never be used to scan
    internal hosts on behalf of a non-admin.
    """
    if user.role != 'admin' and not await has_permission(
        user.id, 'workspace.subagents', await Config.get('user.permissions'), db=db
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    url = (form_data.url or '').strip()
    if not url:
        return {'ok': False, 'error': 'A remote agent URL is required.'}

    ssrf_safe = enforce_ssrf(user.role)
    if ssrf_safe:
        try:
            await asyncio.to_thread(validate_url, url)
        except Exception:
            return {
                'ok': False,
                'error': (
                    'This URL is not allowed. Use a publicly reachable https:// address, '
                    'or ask an admin to enable local network access.'
                ),
            }

    # Re-use the saved credential when the editor did not resend it (it never receives it).
    key = form_data.key or ''
    if not key and form_data.subagent_id:
        existing = await Subagents.get_subagent_by_id(form_data.subagent_id, db=db)
        if existing and (existing.user_id == user.id or user.role == 'admin'):
            stored = ((existing.meta.model_dump() if existing.meta else {}) or {}).get('remote') or {}
            if stored.get('key_encrypted'):
                try:
                    from open_webui.utils.oauth import decrypt_data

                    key = decrypt_data(stored['key_encrypted']) or ''
                except Exception:
                    key = ''

    connection = {
        'url': url,
        'type': 'remote_agent',
        'auth_type': form_data.auth_type or 'bearer',
        'key': key,
        'headers': form_data.headers or {},
    }
    try:
        from open_webui.utils.tools import build_tool_server_headers

        headers, _ = await build_tool_server_headers(connection, request, user)
    except Exception as e:
        return {'ok': False, 'error': f'Could not build credentials: {e}'}

    try:
        card = await fetch_agent_card(url, headers, ssrf_safe=ssrf_safe)
    except Exception as e:
        return {'ok': False, 'error': f'{e}'}

    if card:
        return {'ok': True, 'card': card}
    # No agent card is fine for a generic HTTP agent - it is only used for discovery.
    return {
        'ok': True,
        'card': None,
        'detail': 'Reachable, but no agent card was published at /.well-known/agent.json.',
    }
