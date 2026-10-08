"""Invitations: send, renew, revoke, accept (existing account or new sign-up)."""

from __future__ import annotations

from datetime import timedelta

from conftest import create_org, last_link, signup
from sqlalchemy import update

from countvision_cloud.db import session_factory, utcnow
from countvision_cloud.saas.models import Invite
from countvision_cloud.saas.services import email


def setup_org(client) -> str:
    signup(client, "boss@example.com", "Boss")
    return create_org(client, "Bäckerei Ost")["id"]


def test_invite_new_person_signs_up_from_link(client, client2):
    oid = setup_org(client)
    resp = client.post(f"/api/orgs/{oid}/invites", json={"email": "New@Example.com", "role": "admin"})
    assert resp.status_code == 201
    inv = resp.json()
    assert inv["email"] == "new@example.com" and inv["status"] == "pending" and inv["invited_by"] == "Boss"
    assert "token" not in inv  # the link only goes by email
    mail = email.SENT[-1]
    assert mail.to == "new@example.com" and "Bäckerei Ost" in mail.subject
    token = last_link("new@example.com", "invite")

    preview = client2.get(f"/api/invites/{token}").json()
    assert preview["organization"] == "Bäckerei Ost" and preview["role"] == "admin"
    assert preview["account_exists"] is False and preview["status"] == "pending"

    wrong = client2.post("/api/auth/signup", json={"email": "other@example.com", "name": "N",
                                                   "password": "long-enough-pass", "invite_token": token})
    assert wrong.status_code == 400
    me = signup(client2, "new@example.com", "Nina", invite_token=token)
    assert me["email_verified"] is True  # the emailed link proved the address
    assert [o["role"] for o in me["organizations"]] == ["admin"]
    assert len([m for m in email.SENT if m.to == "new@example.com"]) == 1  # no extra confirm mail
    assert client2.get(f"/api/invites/{token}").json()["status"] == "accepted"
    assert client.get(f"/api/orgs/{oid}/invites").json() == []


def test_existing_user_accepts_and_email_must_match(client, client2, app):
    from fastapi.testclient import TestClient

    oid = setup_org(client)
    signup(client2, "member@example.com")
    client.post(f"/api/orgs/{oid}/invites", json={"email": "member@example.com", "role": "viewer"})
    token = last_link("member@example.com", "invite")
    assert client2.get(f"/api/invites/{token}").json()["account_exists"] is True

    third = TestClient(app, headers={"X-CountVision": "1"})
    signup(third, "intruder@example.com")
    wrong = third.post(f"/api/invites/{token}/accept")
    assert wrong.status_code == 403 and wrong.json()["error"]["code"] == "invite_wrong_user"

    ok = client2.post(f"/api/invites/{token}/accept")
    assert ok.status_code == 200 and ok.json()["org_name"] == "Bäckerei Ost"
    assert client2.get("/api/me").json()["email_verified"] is True
    used = client2.post(f"/api/invites/{token}/accept")
    assert used.status_code == 410 and used.json()["error"]["code"] == "invite_accepted"
    again = client.post(f"/api/orgs/{oid}/invites", json={"email": "member@example.com"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_member"


def test_renew_revoke_and_expire(client, client2):
    oid = setup_org(client)
    client.post(f"/api/orgs/{oid}/invites", json={"email": "x@example.com"})
    first = last_link("x@example.com", "invite")
    client.post(f"/api/orgs/{oid}/invites", json={"email": "x@example.com"})  # resend = new link
    second = last_link("x@example.com", "invite")
    assert first != second
    assert client2.get(f"/api/invites/{first}").json()["status"] == "revoked"
    invites = client.get(f"/api/orgs/{oid}/invites").json()
    assert len(invites) == 1

    assert client.delete(f"/api/orgs/{oid}/invites/{invites[0]['id']}").status_code == 200
    signup(client2, "x@example.com")
    gone = client2.post(f"/api/invites/{second}/accept")
    assert gone.status_code == 410 and gone.json()["error"]["code"] == "invite_revoked"

    client.post(f"/api/orgs/{oid}/invites", json={"email": "x@example.com"})
    third = last_link("x@example.com", "invite")
    with session_factory()() as db:
        db.execute(update(Invite).values(expires_at=utcnow() - timedelta(minutes=1)))
        db.commit()
    late = client2.post(f"/api/invites/{third}/accept")
    assert late.status_code == 410 and late.json()["error"]["code"] == "invite_expired"


def test_unknown_token(client):
    assert client.get("/api/invites/this-token-does-not-exist").status_code == 404


def test_audit_entries_for_invites(client, client2):
    oid = setup_org(client)
    client.post(f"/api/orgs/{oid}/invites", json={"email": "a@example.com", "role": "member"})
    signup(client2, "a@example.com", invite_token=last_link("a@example.com", "invite"))
    actions = [e["action"] for e in client.get(f"/api/orgs/{oid}/audit").json()]
    assert actions == ["invite.accepted", "member.invited", "org.created"]
