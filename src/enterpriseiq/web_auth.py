"""Local pilot identities. Only token hashes are loaded by the API; no public signup."""

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator


@dataclass(frozen=True)
class Identity:
    user_id: str
    workspace: str
    roles: tuple[str, ...]

    def public(self):
        return {"user_id": self.user_id, "workspace": self.workspace, "roles": self.roles}


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def identity_binding(identity):
    """Changing a credential's subject, workspace or role set invalidates old sessions."""
    return token_hash(json.dumps([identity.user_id, identity.workspace, sorted(set(identity.roles))], ensure_ascii=False))


class ProvisionedUser(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    user_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    workspace: str = Field(min_length=1, max_length=64)
    roles: list[Literal["viewer", "investigator", "reviewer"]] = Field(min_length=1, max_length=3)
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("workspace")
    @classmethod
    def stable_workspace(cls, value):
        if value != value.strip():
            raise ValueError("Workspace cannot have leading or trailing whitespace")
        return value

    @field_validator("roles")
    @classmethod
    def unique_roles(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Duplicate roles")
        return value


def load_users():
    path = Path(os.environ.get("ENTERPRISEIQ_USERS_FILE", ".secrets/web-users.json"))
    users = {}
    identities = set()
    try:
        entries = TypeAdapter(list[ProvisionedUser]).validate_python(json.loads(path.read_text(encoding="utf-8")))
    except (ValidationError, json.JSONDecodeError):
        raise ValueError("Invalid web identity configuration") from None
    for entry in entries:
        identity = Identity(entry.user_id, entry.workspace, tuple(entry.roles))
        credential = entry.token_sha256
        if credential in users or (identity.workspace, identity.user_id) in identities:
            raise ValueError("Invalid or duplicate web identity configuration")
        users[credential] = identity
        identities.add((identity.workspace, identity.user_id))
    if not users:
        raise ValueError("Configure at least one local web identity")
    return users


def main():
    parser = argparse.ArgumentParser(description="Provision one local workbench identity; credentials stay in .secrets")
    parser.add_argument("user_id")
    parser.add_argument("--workspace", default="demo")
    parser.add_argument("--roles", nargs="+", required=True, choices=["viewer", "investigator", "reviewer"])
    args = parser.parse_args()
    token = secrets.token_urlsafe(32)
    try:
        entry = ProvisionedUser(user_id=args.user_id, workspace=args.workspace, roles=args.roles, token_sha256=token_hash(token))
    except ValidationError:
        parser.error("Invalid identity, workspace or duplicate roles; no configuration written")
    path = Path(os.environ.get("ENTERPRISEIQ_USERS_FILE", ".secrets/web-users.json"))
    if path.exists():
        try:
            load_users()
        except ValueError:
            parser.error("Existing identity configuration is invalid; no configuration written")
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if any(e["user_id"] == args.user_id and e["workspace"] == args.workspace for e in entries):
        parser.error("Identity already exists; revoke its entry before provisioning a replacement")
    entries.append(entry.model_dump())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    temporary.replace(path)
    # Identical user IDs in different workspace fixtures must not overwrite a token.
    directory = path.parent / token_hash(args.workspace)[:16]
    directory.mkdir(exist_ok=True)
    login_path = directory / (args.user_id + ".login.txt")
    login_path.write_text(token, encoding="utf-8")
    print(f"Identity created. Local login token: {login_path.resolve()}")
