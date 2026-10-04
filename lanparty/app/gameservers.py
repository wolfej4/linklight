"""Dedicated game servers as sibling Docker containers (needs /var/run/docker.sock)."""
import json
import threading

from .config import DATA_DIR

BUILTIN_TEMPLATES = {
    "minecraft": {
        "label": "Minecraft (Java)",
        "image": "itzg/minecraft-server:latest",
        "ports": [["25565", "tcp"]],
        "env": {"EULA": "TRUE", "MEMORY": "4G", "MOTD": "{name}"},
        "data": "/data",
        "note": "Set ONLINE_MODE=FALSE in a custom template if the venue has no internet.",
    },
    "valheim": {
        "label": "Valheim",
        "image": "lloesche/valheim-server:latest",
        "ports": [["2456", "udp"], ["2457", "udp"]],
        "env": {"SERVER_NAME": "{name}", "WORLD_NAME": "LAN", "SERVER_PASS": "lanparty", "SERVER_PUBLIC": "false"},
        "data": "/config",
        "note": "Password is lanparty. Uses two consecutive UDP ports.",
    },
    "cs2": {
        "label": "Counter-Strike 2",
        "image": "joedwards32/cs2:latest",
        "ports": [["27015", "tcp"], ["27015", "udp"]],
        "env": {"CS2_SERVERNAME": "{name}", "CS2_LAN": "1"},
        "data": "/home/steam/cs2-dedicated",
        "note": "Downloads about 60 GB on first start. Do it at home before the event.",
    },
    "factorio": {
        "label": "Factorio",
        "image": "factoriotools/factorio:stable",
        "ports": [["34197", "udp"]],
        "env": {},
        "data": "/factorio",
        "note": "",
    },
    "satisfactory": {
        "label": "Satisfactory",
        "image": "wolveix/satisfactory-server:latest",
        "ports": [["7777", "udp"], ["7777", "tcp"]],
        "env": {"MAXPLAYERS": "8"},
        "data": "/config",
        "note": "Needs about 12 GB of RAM for the server.",
    },
}

_client = None
_jobs: dict[str, str] = {}
_lock = threading.Lock()


def templates() -> dict:
    t = dict(BUILTIN_TEMPLATES)
    custom = DATA_DIR / "server_templates.json"
    if custom.exists():
        try:
            t.update(json.loads(custom.read_text()))
        except (ValueError, OSError):
            pass
    return t


def client():
    global _client
    if _client is None:
        import docker

        c = docker.from_env(timeout=15)
        c.ping()
        _client = c
    return _client


def docker_available() -> tuple[bool, str]:
    global _client
    try:
        client()
        return True, ""
    except Exception as e:  # noqa: BLE001
        _client = None
        return False, str(e)


def port_map(tpl: dict, base: int) -> dict:
    first = int(tpl["ports"][0][0])
    return {f"{p}/{proto}": base + (int(p) - first) for p, proto in tpl["ports"]}


def host_ports(tpl: dict, base: int) -> set[int]:
    return set(port_map(tpl, base).values())


def _split_image(image: str) -> tuple[str, str]:
    last = image.split("/")[-1]
    if ":" in last:
        repo, tag = image.rsplit(":", 1)
        return repo, tag
    return image, "latest"


def create(name: str, container_name: str, tpl_key: str, base_port: int):
    tpl = templates()[tpl_key]

    def work():
        with _lock:
            _jobs[container_name] = "Downloading image"
        try:
            c = client()
            repo, tag = _split_image(tpl["image"])
            c.images.pull(repo, tag=tag)
            with _lock:
                _jobs[container_name] = "Creating container"
            env = {k: str(v).replace("{name}", name) for k, v in tpl.get("env", {}).items()}
            vols = {f"lanparty_{container_name}": {"bind": tpl["data"], "mode": "rw"}} if tpl.get("data") else {}
            c.containers.run(
                f"{repo}:{tag}", name=container_name, detach=True, ports=port_map(tpl, base_port),
                environment=env, volumes=vols, labels={"lanparty.managed": "true"},
                restart_policy={"Name": "unless-stopped"}, stdin_open=True, tty=True,
            )
            with _lock:
                _jobs.pop(container_name, None)
        except Exception as e:  # noqa: BLE001
            with _lock:
                _jobs[container_name] = f"Failed: {e}"

    threading.Thread(target=work, daemon=True).start()


def status(container_name: str) -> tuple[str, str]:
    with _lock:
        job = _jobs.get(container_name)
    if job and not job.startswith("Failed"):
        return "starting", job
    try:
        c = client().containers.get(container_name)
        return c.status, (job or "")
    except Exception as e:  # noqa: BLE001
        if job:
            return "error", job
        if e.__class__.__name__ == "NotFound":
            return "missing", "The container no longer exists. Remove this entry and add it again."
        return "unknown", str(e)


def action(container_name: str, verb: str):
    c = client().containers.get(container_name)
    {"start": c.start, "stop": c.stop, "restart": c.restart}[verb]()


def remove(container_name: str):
    with _lock:
        _jobs.pop(container_name, None)
    try:
        client().containers.get(container_name).remove(force=True)
    except Exception:  # noqa: BLE001
        pass


def logs(container_name: str, tail: int = 120) -> str:
    try:
        return client().containers.get(container_name).logs(tail=tail).decode(errors="replace")
    except Exception as e:  # noqa: BLE001
        return f"Logs unavailable: {e}"
