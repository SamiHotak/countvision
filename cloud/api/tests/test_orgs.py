"""Organizations, roles and the rules around owners."""

from __future__ import annotations

from conftest import PASSWORD, create_org, signup
from sqlalchemy import select

from countvision_cloud.db import session_factory
from countvision_cloud.saas.models import Membership, Role
from countvision_cloud.saas.repositories import orgs as repo


def add_member(org_id: str, email_addr: str, role: Role) -> None:
    """Shortcut for tests: put an existing user into an organization."""
    with session_factory()() as db:
        from countvision_cloud.saas.repositories import users

        user = users.get_user_by_email(db, email_addr)
        repo.add_membership(db, org_id, user.id, role)
        db.commit()


def members(client, org_id: str) -> dict[str, dict]:
    return {m["email"]: m for m in client.get(f"/api/orgs/{org_id}/members").json()}


def test_create_org_makes_owner_and_unique_slug(client):
    signup(client, "o@example.com", "Owner")
    org = create_org(client, "Café Sonne")
    assert org["slug"] == "cafe-sonne" and org["my_role"] == "owner" and org["member_count"] == 1
    second = create_org(client, "Café  Sonne")
    assert second["slug"].startswith("cafe-sonne-") and second["slug"] != org["slug"]
    assert [o["role"] for o in client.get("/api/me").json()["organizations"]] == ["owner", "owner"]


def test_non_members_get_404(client, client2):
    signup(client, "a@example.com")
    org = create_org(client)
    signup(client2, "b@example.com")
    assert client2.get(f"/api/orgs/{org['id']}").status_code == 404
    assert client2.get(f"/api/orgs/{org['id']}/members").status_code == 404
    assert client2.get("/api/orgs/00000000-0000-0000-0000-000000000000").status_code == 404


def test_viewer_cannot_manage(client, client2):
    signup(client, "a@example.com")
    org = create_org(client)
    signup(client2, "v@example.com")
    add_member(org["id"], "v@example.com", Role.VIEWER)
    assert client2.get(f"/api/orgs/{org['id']}/members").status_code == 200  # can read
    assert client2.patch(f"/api/orgs/{org['id']}", json={"name": "Hacked"}).status_code == 403
    assert client2.post(f"/api/orgs/{org['id']}/invites", json={"email": "x@example.com"}).status_code == 403
    assert client2.get(f"/api/orgs/{org['id']}/audit").status_code == 403
    owner_id = members(client, org["id"])["a@example.com"]["id"]
    assert client2.delete(f"/api/orgs/{org['id']}/members/{owner_id}").status_code == 403


def test_admin_rules(client, client2):
    signup(client, "own@example.com")
    org = create_org(client)
    signup(client2, "adm@example.com")
    add_member(org["id"], "adm@example.com", Role.ADMIN)
    oid = org["id"]
    m = members(client, oid)
    # admin may rename, but cannot touch the owner or make owners
    assert client2.patch(f"/api/orgs/{oid}", json={"name": "Renamed"}).status_code == 200
    assert client2.patch(f"/api/orgs/{oid}/members/{m['own@example.com']['id']}", json={"role": "member"}).status_code == 403
    assert client2.delete(f"/api/orgs/{oid}/members/{m['own@example.com']['id']}").status_code == 403
    assert client2.patch(f"/api/orgs/{oid}/members/{m['adm@example.com']['id']}", json={"role": "owner"}).status_code == 403
    assert client2.post(f"/api/orgs/{oid}/invites", json={"email": "x@example.com", "role": "owner"}).status_code == 403
    assert client2.post(f"/api/orgs/{oid}/delete", json={"confirm_name": "Renamed"}).status_code == 403


def test_last_owner_is_protected(client, client2):
    signup(client, "own@example.com")
    org = create_org(client, "Shop")
    oid = org["id"]
    me = members(client, oid)["own@example.com"]["id"]
    demote = client.patch(f"/api/orgs/{oid}/members/{me}", json={"role": "admin"})
    assert demote.status_code == 409 and demote.json()["error"]["code"] == "last_owner"
    assert client.post(f"/api/orgs/{oid}/leave").status_code == 409

    signup(client2, "two@example.com")
    add_member(oid, "two@example.com", Role.MEMBER)
    two = members(client, oid)["two@example.com"]["id"]
    assert client.patch(f"/api/orgs/{oid}/members/{two}", json={"role": "owner"}).status_code == 200
    # now there are two owners: the first may step down and leave
    assert client.patch(f"/api/orgs/{oid}/members/{me}", json={"role": "admin"}).status_code == 200
    assert client.post(f"/api/orgs/{oid}/leave").status_code == 200
    assert client.get(f"/api/orgs/{oid}").status_code == 404


def test_remove_member_and_audit_log(client, client2):
    signup(client, "own@example.com", "Olga")
    org = create_org(client, "Shop")
    oid = org["id"]
    signup(client2, "m@example.com")
    add_member(oid, "m@example.com", Role.MEMBER)
    mid = members(client, oid)["m@example.com"]["id"]
    assert client.patch(f"/api/orgs/{oid}/members/{mid}", json={"role": "viewer"}).status_code == 200
    assert client.delete(f"/api/orgs/{oid}/members/{mid}").status_code == 200
    assert client2.get(f"/api/orgs/{oid}").status_code == 404
    actions = [e["action"] for e in client.get(f"/api/orgs/{oid}/audit").json()]
    assert actions == ["member.removed", "member.role_changed", "org.created"]
    entry = client.get(f"/api/orgs/{oid}/audit").json()[1]
    assert entry["actor"] == "Olga" and entry["meta"] == {"email": "m@example.com", "old": "member", "new": "viewer"}


def test_delete_org_needs_exact_name(client):
    signup(client, "own@example.com")
    org = create_org(client, "Shop Nord")
    oid = org["id"]
    assert client.post(f"/api/orgs/{oid}/delete", json={"confirm_name": "shop"}).status_code == 400
    assert client.post(f"/api/orgs/{oid}/delete", json={"confirm_name": "Shop Nord"}).status_code == 200
    assert client.get("/api/orgs").json() == []
    with session_factory()() as db:
        assert db.scalars(select(Membership)).all() == []


def test_delete_account_blocked_when_last_owner_with_members(client, client2):
    signup(client, "own@example.com")
    org = create_org(client, "Shop")
    signup(client2, "m@example.com")
    add_member(org["id"], "m@example.com", Role.MEMBER)
    resp = client.post("/api/me/delete", json={"password": PASSWORD})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "last_owner"
    # a plain member can delete the account; the org stays
    assert client2.post("/api/me/delete", json={"password": PASSWORD}).status_code == 200
    assert client.get(f"/api/orgs/{org['id']}").json()["member_count"] == 1
