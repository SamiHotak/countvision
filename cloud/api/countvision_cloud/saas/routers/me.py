"""/api/me: the logged-in user, profile, password, delete account."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from ...db import get_db
from ..cookies import clear_session_cookie
from ..deps import current_user, session_token
from ..models import Role, User
from ..schemas import ChangePasswordIn, DeleteAccountIn, MeOut, OkOut, OrgBrief, ProfileIn
from ..services import auth, orgs

router = APIRouter(prefix="/api/me", tags=["me"])


def me_out(db: Session, user: User) -> MeOut:
    return MeOut(
        id=user.id, email=user.email, name=user.name, email_verified=user.email_verified,
        has_password=user.password_hash is not None, google_linked=user.google_sub is not None,
        created_at=user.created_at,
        organizations=[OrgBrief(id=o.id, name=o.name, slug=o.slug, role=Role(r))
                       for o, r in orgs.my_orgs(db, user)],
    )


@router.get("", response_model=MeOut)
def get_me(user: User = Depends(current_user), db: Session = Depends(get_db)) -> MeOut:
    return me_out(db, user)


@router.patch("", response_model=MeOut)
def update_me(body: ProfileIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> MeOut:
    auth.update_profile(db, user, body.name)
    db.commit()
    return me_out(db, user)


@router.post("/password", response_model=OkOut)
def change_password(body: ChangePasswordIn, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> OkOut:
    auth.change_password(db, user, body.current_password, body.new_password,
                         keep_token=session_token(request))
    db.commit()
    return OkOut(message="Password saved. Other devices were logged out.")


@router.post("/delete", response_model=OkOut)
def delete_account(body: DeleteAccountIn, response: Response, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> OkOut:
    """Delete the account and organizations where you are the only member."""
    auth.delete_account(db, user, body.password)
    db.commit()
    clear_session_cookie(response)
    return OkOut(message="Your account was deleted.")
