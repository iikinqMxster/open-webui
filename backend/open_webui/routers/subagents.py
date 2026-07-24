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
from open_webui.models.subagents import (
    SubagentAccessListResponse,
    SubagentAccessResponse,
    SubagentForm,
    SubagentModel,
    SubagentResponse,
    Subagents,
    SubagentUserResponse,
)
from open_webui.utils.access_control import filter_allowed_access_grants, has_permission
from open_webui.utils.auth import get_verified_user
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

PAGE_ITEM_COUNT = 30

router = APIRouter()


def _slugify_handle(value: Optional[str]) -> str:
    """Normalize a user-put id / name into a url-safe handle (e.g. 'Math Agent' -> 'math-agent')."""
    value = re.sub(r'[^a-z0-9]+', '-', (value or '').strip().lower())
    return value.strip('-')


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

    return subagents


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
                **subagent.model_dump(),
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
            return subagent
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
                **subagent.model_dump(),
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
            return subagent
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
    return subagent


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
                return subagent
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
