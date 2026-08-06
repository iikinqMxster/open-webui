import logging
import time
from typing import Optional

from open_webui.internal.db import Base, get_async_db_context
from open_webui.models.access_grants import AccessGrantModel, AccessGrants
from open_webui.models.groups import Groups
from open_webui.models.models import ModelMeta, ModelParams
from open_webui.models.users import User, UserModel, UserResponse, Users
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, BigInteger, Boolean, Column, String, Text, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

####################
# Subagents DB Schema
####################


class Subagent(Base):
    """A reusable, user-configured sub-agent (Workspace > Subagents).

    Shaped like a workspace Model (base_model_id + params + meta) so it can be
    configured with a model, system prompt, tools, skills, filters and capabilities.
    An empty field falls back to a default at delegation time (model -> task model,
    system prompt -> default sub-agent prompt, tools/skills/filters -> inherit parent).
    """

    __tablename__ = 'subagent'

    id = Column(String, primary_key=True, unique=True)  # internal UUID; models bind to this
    user_id = Column(String)
    handle = Column(String, unique=True)  # user-put id (e.g. "math-agent"); how the lead agent refers to it
    name = Column(Text, unique=True)  # display name
    base_model_id = Column(Text, nullable=True)  # model to run on; empty -> task model
    description = Column(Text, nullable=True)  # surfaced to the lead agent like a tool description
    params = Column(JSON)  # see ModelParams (holds `system` prompt, temperature, ...)
    meta = Column(JSON)  # see ModelMeta (holds toolIds, skillIds, filterIds, capabilities, tags)
    is_active = Column(Boolean, default=True)

    updated_at = Column(BigInteger)
    created_at = Column(BigInteger)


class SubagentModel(BaseModel):
    id: str
    user_id: str
    handle: Optional[str] = None
    name: str
    base_model_id: Optional[str] = None
    description: Optional[str] = None
    params: ModelParams = ModelParams()
    meta: ModelMeta = ModelMeta()
    is_active: bool = True
    access_grants: list[AccessGrantModel] = Field(default_factory=list)

    updated_at: int  # timestamp in epoch
    created_at: int  # timestamp in epoch

    model_config = ConfigDict(from_attributes=True)


####################
# Forms
####################


class SubagentUserModel(SubagentModel):
    user: Optional[UserResponse] = None


class SubagentResponse(BaseModel):
    id: str
    user_id: str
    handle: Optional[str] = None
    name: str
    base_model_id: Optional[str] = None
    description: Optional[str] = None
    meta: ModelMeta = ModelMeta()
    is_active: bool = True
    access_grants: list[AccessGrantModel] = Field(default_factory=list)
    updated_at: int  # timestamp in epoch
    created_at: int  # timestamp in epoch


class SubagentUserResponse(SubagentResponse):
    user: Optional[UserResponse] = None

    model_config = ConfigDict(extra='allow')


class SubagentAccessResponse(SubagentUserResponse):
    write_access: Optional[bool] = False


class SubagentForm(BaseModel):
    model_config = ConfigDict(extra='ignore')

    id: str
    handle: Optional[str] = None
    name: str
    base_model_id: Optional[str] = None
    description: Optional[str] = None
    params: ModelParams = ModelParams()
    meta: ModelMeta = ModelMeta()
    is_active: bool = True
    access_grants: Optional[list[dict]] = None


class SubagentListResponse(BaseModel):
    items: list[SubagentUserResponse] = []
    total: int = 0


class SubagentAccessListResponse(BaseModel):
    items: list[SubagentAccessResponse] = []
    total: int = 0


class SubagentsTable:
    async def _get_access_grants(
        self, subagent_id: str, db: Optional[AsyncSession] = None
    ) -> list[AccessGrantModel]:
        return await AccessGrants.get_grants_by_resource('subagent', subagent_id, db=db)

    async def _to_subagent_model(
        self,
        subagent: Subagent,
        access_grants: Optional[list[AccessGrantModel]] = None,
        db: Optional[AsyncSession] = None,
    ) -> SubagentModel:
        subagent_data = SubagentModel.model_validate(subagent).model_dump(exclude={'access_grants'})
        subagent_data['access_grants'] = (
            access_grants
            if access_grants is not None
            else await self._get_access_grants(subagent_data['id'], db=db)
        )
        return SubagentModel.model_validate(subagent_data)

    async def insert_new_subagent(
        self,
        user_id: str,
        form_data: SubagentForm,
        db: Optional[AsyncSession] = None,
    ) -> Optional[SubagentModel]:
        async with get_async_db_context(db) as db:
            try:
                result = Subagent(
                    **{
                        **form_data.model_dump(exclude={'access_grants'}),
                        'user_id': user_id,
                        'updated_at': int(time.time()),
                        'created_at': int(time.time()),
                    }
                )
                db.add(result)
                await db.commit()
                await db.refresh(result)
                await AccessGrants.set_access_grants('subagent', result.id, form_data.access_grants, db=db)
                if result:
                    return await self._to_subagent_model(result, db=db)
                else:
                    return None
            except Exception as e:
                log.exception(f'Error creating a new subagent: {e}')
                return None

    async def get_subagent_by_id(self, id: str, db: Optional[AsyncSession] = None) -> Optional[SubagentModel]:
        try:
            async with get_async_db_context(db) as db:
                subagent = await db.get(Subagent, id)
                return await self._to_subagent_model(subagent, db=db) if subagent else None
        except Exception:
            return None

    async def get_subagent_by_name(self, name: str, db: Optional[AsyncSession] = None) -> Optional[SubagentModel]:
        try:
            async with get_async_db_context(db) as db:
                result = await db.execute(select(Subagent).filter_by(name=name))
                subagent = result.scalars().first()
                return await self._to_subagent_model(subagent, db=db) if subagent else None
        except Exception:
            return None

    async def get_subagent_by_handle(
        self, handle: str, db: Optional[AsyncSession] = None
    ) -> Optional[SubagentModel]:
        try:
            async with get_async_db_context(db) as db:
                result = await db.execute(select(Subagent).filter_by(handle=handle))
                subagent = result.scalars().first()
                return await self._to_subagent_model(subagent, db=db) if subagent else None
        except Exception:
            return None

    async def get_subagents(self, db: Optional[AsyncSession] = None) -> list[SubagentUserModel]:
        async with get_async_db_context(db) as db:
            result = await db.execute(select(Subagent).order_by(Subagent.updated_at.desc()))
            all_subagents = result.scalars().all()

            user_ids = list(set(subagent.user_id for subagent in all_subagents))
            subagent_ids = [subagent.id for subagent in all_subagents]

            users = await Users.get_users_by_user_ids(user_ids, db=db) if user_ids else []
            users_dict = {user.id: user for user in users}
            grants_map = await AccessGrants.get_grants_by_resources('subagent', subagent_ids, db=db)

            subagents = []
            for subagent in all_subagents:
                user = users_dict.get(subagent.user_id)
                subagents.append(
                    SubagentUserModel.model_validate(
                        {
                            **(
                                await self._to_subagent_model(
                                    subagent,
                                    access_grants=grants_map.get(subagent.id, []),
                                    db=db,
                                )
                            ).model_dump(),
                            'user': user.model_dump() if user else None,
                        }
                    )
                )
            return subagents

    async def get_subagents_by_ids(
        self, ids: list[str], active_only: bool = True, db: Optional[AsyncSession] = None
    ) -> list[SubagentModel]:
        """Fetch sub-agents by id (used to inject the attached list into the delegate_task spec)."""
        if not ids:
            return []
        try:
            async with get_async_db_context(db) as db:
                stmt = select(Subagent).filter(Subagent.id.in_(ids))
                if active_only:
                    stmt = stmt.filter(Subagent.is_active == True)  # noqa: E712
                result = await db.execute(stmt)
                rows = result.scalars().all()
                grants_map = await AccessGrants.get_grants_by_resources('subagent', [r.id for r in rows], db=db)
                return [
                    await self._to_subagent_model(row, access_grants=grants_map.get(row.id, []), db=db)
                    for row in rows
                ]
        except Exception:
            return []

    async def get_subagents_by_user_id(
        self, user_id: str, permission: str = 'write', db: Optional[AsyncSession] = None
    ) -> list[SubagentUserModel]:
        subagents = await self.get_subagents(db=db)
        user_groups = await Groups.get_groups_by_member_id(user_id, db=db)
        user_group_ids = {group.id for group in user_groups}

        result = []
        for subagent in subagents:
            if subagent.user_id == user_id:
                result.append(subagent)
            elif await AccessGrants.has_access(
                user_id=user_id,
                resource_type='subagent',
                resource_id=subagent.id,
                permission=permission,
                user_group_ids=user_group_ids,
                db=db,
            ):
                result.append(subagent)
        return result

    async def search_subagents(
        self,
        user_id: str,
        filter: dict = {},
        skip: int = 0,
        limit: int = 30,
        db: Optional[AsyncSession] = None,
    ) -> SubagentListResponse:
        try:
            async with get_async_db_context(db) as db:
                stmt = select(Subagent, User).outerjoin(User, User.id == Subagent.user_id)

                if filter:
                    query_key = filter.get('query')
                    if query_key:
                        stmt = stmt.filter(
                            or_(
                                Subagent.name.ilike(f'%{query_key}%'),
                                Subagent.handle.ilike(f'%{query_key}%'),
                                Subagent.description.ilike(f'%{query_key}%'),
                                User.name.ilike(f'%{query_key}%'),
                                User.email.ilike(f'%{query_key}%'),
                            )
                        )

                    view_option = filter.get('view_option')
                    if view_option == 'created':
                        stmt = stmt.filter(Subagent.user_id == user_id)
                    elif view_option == 'shared':
                        stmt = stmt.filter(Subagent.user_id != user_id)

                    stmt = AccessGrants.has_permission_filter(
                        db=db,
                        query=stmt,
                        DocumentModel=Subagent,
                        filter=filter,
                        resource_type='subagent',
                        permission='read',
                    )

                stmt = stmt.order_by(Subagent.updated_at.desc())

                count_result = await db.execute(select(func.count()).select_from(stmt.subquery()))
                total = count_result.scalar()

                if skip:
                    stmt = stmt.offset(skip)
                if limit:
                    stmt = stmt.limit(limit)

                result = await db.execute(stmt)
                items = result.all()

                subagent_ids = [subagent.id for subagent, _ in items]
                grants_map = await AccessGrants.get_grants_by_resources('subagent', subagent_ids, db=db)

                subagents = []
                for subagent, user in items:
                    subagents.append(
                        SubagentUserResponse(
                            **(
                                await self._to_subagent_model(
                                    subagent,
                                    access_grants=grants_map.get(subagent.id, []),
                                    db=db,
                                )
                            ).model_dump(),
                            user=(UserResponse(**UserModel.model_validate(user).model_dump()) if user else None),
                        )
                    )

                return SubagentListResponse(items=subagents, total=total)
        except Exception as e:
            log.exception(f'Error searching subagents: {e}')
            return SubagentListResponse(items=[], total=0)

    async def update_subagent_by_id(
        self, id: str, updated: dict, db: Optional[AsyncSession] = None
    ) -> Optional[SubagentModel]:
        try:
            async with get_async_db_context(db) as db:
                access_grants = updated.pop('access_grants', None)
                await db.execute(update(Subagent).filter_by(id=id).values(**updated, updated_at=int(time.time())))
                await db.commit()
                if access_grants is not None:
                    await AccessGrants.set_access_grants('subagent', id, access_grants, db=db)

                subagent = await db.get(Subagent, id)
                await db.refresh(subagent)
                return await self._to_subagent_model(subagent, db=db)
        except Exception:
            return None

    async def toggle_subagent_by_id(self, id: str, db: Optional[AsyncSession] = None) -> Optional[SubagentModel]:
        async with get_async_db_context(db) as db:
            try:
                result = await db.execute(select(Subagent).filter_by(id=id))
                subagent = result.scalars().first()
                if not subagent:
                    return None

                subagent.is_active = not subagent.is_active
                subagent.updated_at = int(time.time())
                await db.commit()
                await db.refresh(subagent)

                return await self._to_subagent_model(subagent, db=db)
            except Exception:
                return None

    async def delete_subagent_by_id(self, id: str, db: Optional[AsyncSession] = None) -> bool:
        try:
            async with get_async_db_context(db) as db:
                await AccessGrants.revoke_all_access('subagent', id, db=db)
                await db.execute(delete(Subagent).filter_by(id=id))
                await db.commit()

                return True
        except Exception:
            return False


Subagents = SubagentsTable()
