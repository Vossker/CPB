from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from cpb.database import get_db
from cpb.errors import AuthenticationError, AuthorizationError
from cpb.models import ApiKey, Node, NodeStatus, User, UserStatus
from cpb.security import hash_token

DbSession = Annotated[Session, Depends(get_db)]


def current_user(
    session: DbSession,
    api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> User:
    if not api_key:
        raise AuthenticationError("X-API-Key header is required")
    key = session.scalar(
        select(ApiKey).where(ApiKey.token_hash == hash_token(api_key), ApiKey.revoked_at.is_(None))
    )
    if not key:
        raise AuthenticationError("invalid API key")
    user = session.get(User, key.user_id)
    if not user or user.status != UserStatus.active:
        raise AuthorizationError("user account is not active")
    return user


def current_node(
    session: DbSession,
    worker_token: Annotated[str | None, Header(alias="X-Worker-Token")] = None,
) -> Node:
    if not worker_token:
        raise AuthenticationError("X-Worker-Token header is required")
    node = session.scalar(select(Node).where(Node.worker_token_hash == hash_token(worker_token)))
    if not node:
        raise AuthenticationError("invalid worker token")
    if node.status == NodeStatus.disabled:
        raise AuthorizationError("node is disabled")
    return node


CurrentUser = Annotated[User, Depends(current_user)]
CurrentNode = Annotated[Node, Depends(current_node)]
LeaseToken = Annotated[str, Header(alias="X-Lease-Token")]
