"""Add agents from the official ACP registry (https://github.com/agentclientprotocol/registry).

The registry lists ~45 coding agents that speak the Agent Client Protocol, each with a pinned
version and how to start it: an npm package (`npx`), a release archive per platform with a
sha256 (`binary`), or a PyPI package (`uvx`, not supported here). `config_for()` turns one entry
into a Thinair agent config:

- npm: `npm install -g <package@version>` at image build; the command is the package's `bin`
  (read from the npm registry, since the ACP registry only says "npx <package>").
- binary: Linux x86_64 / aarch64 archives become `download:` (fetched and checksum-checked in a
  build stage, see `cli_adapter._fetch_stage`).
- HOME points into the agent's login volume, so whatever the agent saves when you log in (its
  config dir under ~) is kept between runs, per user, like the built-in agents.

The registry has no login information (agents announce it at runtime), so the generated
`login_command` starts the agent's own CLI; `thinair auth <id>` runs it interactively.
"""

from datetime import date
from typing import Any

import requests

from thinair.agents.config import DOWNLOAD_DIR, AgentConfig

REGISTRY_URL = "https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json"
NPM_URL = "https://registry.npmjs.org/{name}/{version}"
PLATFORMS = ("linux-x86_64", "linux-aarch64")
TIMEOUT = 30


class RegistryError(Exception):
    pass


def _get_json(url: str) -> Any:
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        raise RegistryError(f"Could not read {url}: {type(e).__name__}") from None


def fetch_registry(get=None) -> list[dict]:  # noqa: ANN001
    data = (get or _get_json)(REGISTRY_URL)
    agents = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, list):
        raise RegistryError("The ACP registry returned an unexpected format.")
    return [a for a in agents if isinstance(a, dict) and isinstance(a.get("id"), str)]


def how(entry: dict) -> str:
    """How Thinair would install it: 'npm', 'download', or why it can't."""
    dist = entry.get("distribution") or {}
    if "npx" in dist:
        return "npm"
    if any(p in (dist.get("binary") or {}) for p in PLATFORMS):
        return "download"
    if "uvx" in dist:
        return "unsupported (Python/uvx)"
    return "unsupported (no Linux build)"


def split_package(spec: str) -> tuple[str, str]:
    """'@scope/name@1.2.3' -> ('@scope/name', '1.2.3')."""
    at = spec.rfind("@")
    if at <= 0:
        raise RegistryError(f"Package {spec!r} has no pinned version.")
    return spec[:at], spec[at + 1 :]


def npm_bin(name: str, version: str, get=None) -> str:  # noqa: ANN001
    """The command a package installs (its `bin`), as `npx` would pick it."""
    meta = (get or _get_json)(NPM_URL.format(name=name.replace("/", "%2F"), version=version))
    bins = meta.get("bin") if isinstance(meta, dict) else None
    base = name.split("/")[-1]
    if isinstance(bins, str):
        return base
    if isinstance(bins, dict) and bins:
        if len(bins) == 1:
            return next(iter(bins))
        if base in bins:
            return base
        if len(set(bins.values())) == 1:  # several names for one program: take the shortest
            return min(bins, key=lambda k: (len(k), k))
    raise RegistryError(f"Can't tell which command {name}@{version} installs (bin: {bins!r}).")


def config_for(entry: dict, get=None) -> dict:  # noqa: ANN001
    """A Thinair agent config (as a dict, ready for YAML) for one registry entry."""
    dist = entry.get("distribution") or {}
    aid, name, version = entry["id"], entry.get("name") or entry["id"], entry.get("version")
    env: dict[str, str] = {}
    cfg: dict[str, Any] = {"id": aid, "name": name, "version": version}
    kind = how(entry)
    if kind == "npm":
        npx = dist["npx"]
        pkg, ver = split_package(npx["package"])
        binary = npm_bin(pkg, ver, get)
        cfg["install"] = f"npm install -g {pkg}@{ver}"
        command = [binary, *npx.get("args", [])]
        env.update(npx.get("env") or {})
        login = [binary]
    elif kind == "download":
        targets = {p: dist["binary"][p] for p in PLATFORMS if p in dist["binary"]}
        rels = {p: t["cmd"].removeprefix("./") for p, t in targets.items()}
        argsets = {tuple(t.get("args") or []) for t in targets.values()}
        if len(argsets) != 1:
            raise RegistryError(f"{aid}: the x86_64 and arm64 builds take different arguments.")
        if any(r.startswith("/") or ".." in r.split("/") for r in rels.values()):
            raise RegistryError(f"{aid}: unexpected command path in the registry.")
        same = len(set(rels.values())) == 1
        cfg["download"] = {
            p: {
                "url": t["archive"],
                **({"sha256": t["sha256"]} if t.get("sha256") else {}),
                **({} if same else {"cmd": rels[p]}),
            }
            for p, t in targets.items()
        }
        rel = next(iter(rels.values())) if same else "thinair-agent-cmd"
        binary = f"{DOWNLOAD_DIR}/{rel}"
        command = [binary, *argsets.pop()]
        for t in targets.values():
            env.update(t.get("env") or {})
        login = [binary]
    else:
        raise RegistryError(f"{aid} can't be installed by Thinair: {kind}.")
    env["HOME"] = "/thinair-auth"  # the agent's own config and login live in its volume
    cfg.update(
        {
            "needs_node": True,  # Thinair's ACP driver runs on Node.js
            "protocol": "acp",
            "command": command,
            "env": env,
            "auth": {
                "config_dir": "/thinair-auth",
                "login_command": login,
                "check_command": None,
                "env_passthrough": [],
                "login_help": (
                    f"This starts {name} itself. Use its own login (often a /login command, "
                    "or a `login` subcommand), then exit it. If it only works with an API key, "
                    "add the key's variable to `env_passthrough` in this file and to "
                    "Thinair's .env."
                ),
                "hint": (
                    f"{name} is not logged in for this Thinair server. Run `thinair auth {aid}` "
                    "on the server."
                ),
            },
        }
    )
    AgentConfig.model_validate(cfg)  # never write a config Thinair would refuse to load
    return cfg


def to_yaml(cfg: dict, entry: dict) -> str:
    import yaml

    header = [
        f"# {cfg['name']} {cfg.get('version') or ''}, from the ACP registry on {date.today()}",
        f"# ({REGISTRY_URL}). Generated by `thinair agent add {cfg['id']}`.",
    ]
    if entry.get("repository") or entry.get("website"):
        header.append(f"# Project: {entry.get('website') or entry.get('repository')}")
    if cfg.get("download") and any("sha256" not in d for d in cfg["download"].values()):
        header.append("# Note: the registry gives no checksum for this download.")
    header.append("# Login steps are the agent's own; edit `auth:` if it needs something else.")
    return "\n".join(header) + "\n" + yaml.safe_dump(cfg, sort_keys=False, width=100)
