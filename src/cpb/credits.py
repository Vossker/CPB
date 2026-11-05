import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cpb.config import get_settings
from cpb.errors import ConflictError, InsufficientCreditsError, NotFoundError
from cpb.models import (
    CreditAccount,
    CreditTransaction,
    TransactionType,
    User,
    UserStatus,
)


def account_for_user(session: Session, user_id: uuid.UUID, *, lock: bool = False) -> CreditAccount:
    statement = select(CreditAccount).where(CreditAccount.user_id == user_id)
    if lock:
        statement = statement.with_for_update()
    account = session.scalar(statement)
    if not account:
        raise NotFoundError("credit account not found")
    return account


def balance_for_account(session: Session, account_id: uuid.UUID) -> Decimal:
    amount = session.scalar(
        select(func.coalesce(func.sum(CreditTransaction.amount), 0)).where(
            CreditTransaction.account_id == account_id
        )
    )
    return Decimal(amount or 0)


def append_transaction(
    session: Session,
    account: CreditAccount,
    amount: Decimal | int,
    transaction_type: TransactionType,
    reference_type: str,
    reference_id: str,
    idempotency_key: str,
    *,
    allow_negative_balance: bool = False,
) -> CreditTransaction:
    amount = Decimal(amount)
    existing = session.scalar(
        select(CreditTransaction).where(CreditTransaction.idempotency_key == idempotency_key)
    )
    if existing:
        if (
            existing.account_id != account.id
            or existing.amount != amount
            or existing.transaction_type != transaction_type
        ):
            raise ConflictError("idempotency key was already used for another transaction")
        return existing
    if not allow_negative_balance and balance_for_account(session, account.id) + amount < 0:
        raise InsufficientCreditsError("insufficient credits")
    transaction = CreditTransaction(
        account_id=account.id,
        amount=amount,
        transaction_type=transaction_type,
        reference_type=reference_type,
        reference_id=reference_id,
        idempotency_key=idempotency_key,
    )
    session.add(transaction)
    session.flush()
    return transaction


def create_user_with_account(
    session: Session,
    username: str,
    email: str,
    *,
    status: UserStatus = UserStatus.active,
    signup_bonus: int | None = None,
) -> User:
    user = User(username=username, email=email, status=status)
    session.add(user)
    session.flush()
    account = CreditAccount(user_id=user.id)
    session.add(account)
    session.flush()
    bonus = get_settings().signup_bonus if signup_bonus is None else signup_bonus
    if bonus:
        append_transaction(
            session,
            account,
            bonus,
            TransactionType.signup_bonus,
            "user",
            str(user.id),
            f"signup:{user.id}",
            allow_negative_balance=True,
        )
    return user


def ensure_platform_user(session: Session) -> User:
    settings = get_settings()
    user = session.scalar(select(User).where(User.username == settings.platform_username))
    if user:
        return user
    return create_user_with_account(
        session,
        settings.platform_username,
        "platform@cpb.invalid",
        status=UserStatus.platform,
        signup_bonus=0,
    )
