"""Where the key comes from, and everything else a command needs to know.

The API key is looked for in this order:

  1. ``DMZAGENT_API_KEY`` (or ``DMZAGENT_APP_KEY``) in the environment;
  2. an env file: ``--env-file``, ``DMZAGENT_ENV_FILE``, ``./app/.env``, ``./.env``
     (the file ``dmz apply`` writes for an application);
  3. a saved profile from ``dmz auth set`` in ``$XDG_CONFIG_HOME/dmz/credentials.json``.

The base URL follows the same order, then defaults to the public endpoint.
Nothing here prints a key; :func:`dmz.client.fingerprint` names one safely.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .client import ConfigError, Platform, fingerprint  # noqa: F401

DEFAULT_BASE_URL = "https://api.dmzagent.com"
KEY_VARS = ("DMZAGENT_API_KEY", "DMZAGENT_APP_KEY")
DEFAULT_PROFILE = "default"


# -- the credentials file --------------------------------------------------

def config_dir() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / "dmz"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def load_credentials() -> dict:
    path = credentials_path()
    if not path.exists():
        return {"profiles": {}}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        raise ConfigError(f"{path} is not valid JSON; fix or remove it")
    data.setdefault("profiles", {})
    return data


def save_profile(name: str, api_key: str, base_url: str | None = None) -> Path:
    data = load_credentials()
    data["profiles"][name] = {
        "api_key": api_key,
        "base_url": (base_url or DEFAULT_BASE_URL).rstrip("/"),
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", opener=lambda p, f: os.open(p, f, 0o600)) as fh:
        json.dump(data, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def delete_profile(name: str) -> bool:
    data = load_credentials()
    if name not in data["profiles"]:
        return False
    del data["profiles"][name]
    path = credentials_path()
    with open(path, "w", opener=lambda p, f: os.open(p, f, 0o600)) as fh:
        json.dump(data, fh, indent=2)
    return True


# -- env files -------------------------------------------------------------

def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def find_env_file(explicit: str | None = None) -> Path | None:
    """The env file to read: an explicit path, DMZAGENT_ENV_FILE, then the
    conventional places an example writes to."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise ConfigError(f"env file not found: {path}")
        return path.resolve()
    configured = os.environ.get("DMZAGENT_ENV_FILE")
    if configured:
        path = Path(configured).expanduser()
        if path.exists():
            return path.resolve()
    for candidate in (Path("app/.env"), Path(".env")):
        if candidate.exists():
            return candidate.resolve()
    return None


# -- the resolved context ----------------------------------------------------

@dataclass
class Context:
    api_key: str | None
    base_url: str
    key_source: str
    env_file: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    profile: str = DEFAULT_PROFILE
    workspace_id: str | None = None
    division_id: str | None = None
    log: object = None
    _platform: Platform | None = field(default=None, repr=False)
    _who: dict | None = field(default=None, repr=False)

    def require_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                "no API key. Set DMZAGENT_API_KEY, point --env-file at a file that has one, "
                "or save one with `dmz auth set`.")
        if not self.api_key.startswith("ck_"):
            raise ConfigError(f"the key from {self.key_source} does not look like a DMZAgent key "
                              "(they start with ck_)")
        return self.api_key

    @property
    def platform(self) -> Platform:
        if self._platform is None:
            self._platform = Platform(self.require_key(), self.base_url, log=self.log or (lambda m: None))
        return self._platform

    def whoami(self) -> dict:
        """The principal behind the key; fills workspace and division."""
        if self._who is None:
            who = self.platform.whoami()
            self.workspace_id = who["workspace_id"]
            if not self.division_id:
                try:
                    self.division_id = self.platform.division_id_for_key()
                except ConfigError:
                    self.division_id = None
            who["division_id"] = self.division_id
            self._who = who
        return self._who

    def fingerprint(self) -> str:
        return fingerprint(self.api_key or "")


def resolve(*, base_url: str | None = None, env_file: str | None = None, profile: str | None = None,
            log=None) -> Context:
    """Build a Context from the environment, an env file and the saved profile."""
    profile = profile or os.environ.get("DMZ_PROFILE") or DEFAULT_PROFILE
    env_path = find_env_file(env_file)
    env = read_env_file(env_path) if env_path else {}
    creds = load_credentials()["profiles"].get(profile) or {}

    api_key, source = None, "nowhere"
    for var in KEY_VARS:
        if os.environ.get(var, "").strip():
            api_key, source = os.environ[var].strip(), f"${var}"
            break
    if not api_key and env_path:
        for var in KEY_VARS:
            if env.get(var):
                api_key, source = env[var], f"{_pretty(env_path)} ({var})"
                break
    if not api_key and creds.get("api_key"):
        api_key, source = creds["api_key"], f"profile {profile!r} in {_pretty(credentials_path())}"

    url = (base_url or os.environ.get("DMZAGENT_BASE_URL") or env.get("DMZAGENT_BASE_URL")
           or creds.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    division = os.environ.get("DMZAGENT_DIVISION_ID") or env.get("DMZAGENT_DIVISION_ID") or None
    workspace = os.environ.get("DMZAGENT_WORKSPACE_ID") or env.get("DMZAGENT_WORKSPACE_ID") or None
    if division:
        # The toolkit's division lookup honours this and skips a round trip.
        os.environ.setdefault("DMZAGENT_DIVISION_ID", division)
    return Context(api_key=api_key, base_url=url, key_source=source, env_file=env_path, env=env,
                   profile=profile, workspace_id=workspace, division_id=division, log=log)


def _pretty(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        try:
            return str(path.relative_to(Path.cwd()))
        except ValueError:
            return str(path)


def canonical_subject(division_id: str | None, text: str) -> str:
    """The canonical subject id for what a person typed, the way the platform's
    MCP server canonicalizes a logical id.

    ``subject:<division>:...`` passes through. ``customer:alice`` becomes
    ``subject:<division>:customer:alice``; a bare ``alice`` becomes
    ``subject:<division>:alice``.
    """
    text = text.strip()
    if text.startswith("subject:"):
        return text
    if not division_id:
        raise ConfigError(f"cannot canonicalize {text!r} without a division; pass the full "
                          "subject:<division>:<type>:<slug> or set DMZAGENT_DIVISION_ID")
    return f"subject:{division_id}:{text}"
