"""Request and response models of the platform API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from .models import Role


def _clean_name(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("Must not be empty")
    return value


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --- auth -----------------------------------------------------------------------------------

class SignupIn(_In):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=200)
    invite_token: str | None = Field(default=None, max_length=200)

    _name = field_validator("name")(_clean_name)


class LoginIn(_In):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class TokenIn(_In):
    token: str = Field(min_length=10, max_length=200)


class EmailIn(_In):
    email: EmailStr


class ResetPasswordIn(_In):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordIn(_In):
    current_password: str | None = Field(default=None, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


class ProfileIn(_In):
    name: str = Field(min_length=1, max_length=120)

    _name = field_validator("name")(_clean_name)


class DeleteAccountIn(_In):
    password: str | None = Field(default=None, max_length=200)


class ProvidersOut(BaseModel):
    password: bool = True
    google: bool


# --- me -------------------------------------------------------------------------------------

class OrgBrief(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    role: Role


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    email_verified: bool
    has_password: bool
    google_linked: bool
    created_at: datetime
    organizations: list[OrgBrief]


# --- organizations --------------------------------------------------------------------------

class OrgIn(_In):
    name: str = Field(min_length=2, max_length=120)

    _name = field_validator("name")(_clean_name)


class OrgDeleteIn(_In):
    confirm_name: str = Field(min_length=1, max_length=120)


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime
    my_role: Role
    member_count: int


class MemberOut(BaseModel):
    id: uuid.UUID  # membership id
    user_id: uuid.UUID
    email: str
    name: str
    role: Role
    joined_at: datetime
    is_me: bool


class RoleIn(_In):
    role: Role


class InviteIn(_In):
    email: EmailStr
    role: Role = Role.MEMBER


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    role: Role
    status: str
    invited_by: str | None
    created_at: datetime
    expires_at: datetime


class InvitePreviewOut(BaseModel):
    organization: str
    email: str
    role: Role
    invited_by: str | None
    status: str
    expires_at: datetime
    account_exists: bool


class AcceptOut(BaseModel):
    org_id: uuid.UUID
    org_name: str


class AuditOut(BaseModel):
    id: int
    action: str
    actor: str | None
    target_type: str | None
    target_id: str | None
    meta: dict[str, Any]
    created_at: datetime


class OkOut(BaseModel):
    ok: bool = True
    message: str | None = None
