"""Local pilot identities. Only token hashes are loaded by the API; no public signup."""

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets


@dataclass(frozen=True)
class Identity:
    user_id: str
    workspace: str
    roles: tuple[str, ...]

    def public(self):
        return {"user_id": self.user_id, "workspace": self.workspace, "roles": self.roles}


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def load_users():
    path = Path(os.environ.get("ENTERPRISEIQ_USERS_FILE", ".secrets/web-users.json"))
    users = {}
    identities = set()
    for entry in json.loads(path.read_text(encoding="utf-8")):
        identity = Identity(entry["user_id"], entry["workspace"], tuple(entry["roles"]))
        credential = entry["token_sha256"]
        if (not identity.user_id or not identity.workspace or not identity.roles
                or not set(identity.roles) <= {"viewer", "investigator", "reviewer"}
                or len(credential) != 64 or any(c not in "0123456789abcdef" for c in credential)
                or credential in users or (identity.workspace, identity.user_id) in identities):
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
    if not args.user_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in args.user_id):
        parser.error("Use an ASCII user ID with letters, numbers, hyphens or underscores")
    directory = Path(".secrets")
    directory.mkdir(exist_ok=True)
    path = directory / "web-users.json"
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if any(e["user_id"] == args.user_id and e["workspace"] == args.workspace for e in entries):
        parser.error("Identity already exists; revoke its entry before provisioning a replacement")
    token = secrets.token_urlsafe(32)
    entries.append({"user_id": args.user_id, "workspace": args.workspace, "roles": args.roles, "token_sha256": token_hash(token)})
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    temporary.replace(path)
    login_path = directory / (args.user_id + ".login.txt")
    login_path.write_text(token, encoding="utf-8")
    print(f"Identity created. Local login token: {login_path.resolve()}")
