"""Build the proxy and agent images and manage the isolated network."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tarfile
from pathlib import Path
from typing import Any

from . import ROOT
from .config import API_HOST, Experiment, pins
from .util import docker, download, log, run, sha256_file

# Variables that Zeroshot or Codex set per process; everything else from the image ENV is mirrored.
_NOT_MIRRORED = {"HOME", "HOSTNAME"}
# Non-interactive settings the upstream mini-SWE-agent ProgramBench baseline exported, and the
# image's plain /tmp: Zeroshot points Codex's TMPDIR into its run directory, which tool commands
# should neither write into nor learn about.
_BASELINE_ENV = {"PAGER": "cat", "MANPAGER": "cat", "LESS": "-R", "PIP_PROGRESS_BAR": "off", "TQDM_DISABLE": "1", "TMPDIR": "/tmp"}


def _extract(archive: Path, member: str, dest: Path) -> None:
    with tarfile.open(archive) as tar:
        source = tar.extractfile(member)
        if source is None:
            raise RuntimeError(f"{member} missing from {archive}")
        dest.write_bytes(source.read())
    dest.chmod(0o755)


def ensure_image(reference: str) -> None:
    if docker("image", "inspect", reference, check=False).strip() in ("", "[]"):
        log(f"pulling {reference}")
        docker("pull", "--quiet", reference, timeout=3600)


def image_env(reference: str) -> dict[str, str]:
    env = json.loads(docker("image", "inspect", reference, "--format", "{{json .Config.Env}}"))
    return dict(item.split("=", 1) for item in env or [])


def codex_config(task_env: dict[str, str]) -> str:
    lines = [f"{key} = {json.dumps(value)}" for key, value in sorted(tool_environment(task_env).items())]
    template = (ROOT / "agent" / "codex-config.toml.in").read_text()
    return template.replace("@SET@", "\n".join(lines))


def tool_environment(task_env: dict[str, str]) -> dict[str, str]:
    """The task image's environment for tool commands, with the upstream mini-SWE-agent harness's settings."""
    mirrored = {k: v for k, v in task_env.items() if k not in _NOT_MIRRORED}
    for key, value in _BASELINE_ENV.items():
        mirrored.setdefault(key, value)
    return mirrored


def proxy_filter(host: str = API_HOST) -> bytes:
    """tinyproxy's allowlist: the one model API host (proxy/filter holds it)."""
    return f"^{re.escape(host)}$\n".encode()


def build_proxy(cache: Path) -> str:
    base = pins()["proxy_base_image"]
    files = {name: (ROOT / "proxy" / name).read_bytes() for name in ("Dockerfile", "tinyproxy.conf")}
    files["filter"] = proxy_filter()
    digest = hashlib.sha256()
    for data in files.values():
        digest.update(data)
    digest.update(base.encode())
    tag = f"zsbench-proxy:{digest.hexdigest()[:16]}"
    if docker("image", "inspect", tag, check=False).strip() not in ("", "[]"):
        return tag
    log(f"building {tag}")
    context = cache / "build" / tag.replace(":", "-")
    context.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (context / name).write_bytes(data)
    docker("build", "--quiet", "--build-arg", f"BASE_IMAGE={base}", "-t", tag, str(context), timeout=1800)
    return tag


def build_agent(exp: Experiment, cache: Path) -> tuple[str, dict[str, str]]:
    """Return the agent image tag and the rendered Codex config (for the manifest)."""
    tag, info = _build_agent(exp, cache)
    return _reaper_image(tag, info, cache) if exp.reap_orphans else (tag, info)


def _build_agent(exp: Experiment, cache: Path) -> tuple[str, dict[str, str]]:
    pin = pins()
    ensure_image(exp.task_image)
    config = codex_config(image_env(exp.task_image))
    adjustments = json.dumps({"reference_path": exp.reference_path, "doc_fixes": exp.doc_fixes}, sort_keys=True, indent=2)
    digest = hashlib.sha256()
    digest.update(exp.task_image.encode())
    digest.update(config.encode())
    digest.update(adjustments.encode())
    for name in ("Dockerfile", "prepare-task.py"):
        digest.update((ROOT / "agent" / name).read_bytes())
    for tool in ("zeroshot", "codex"):
        digest.update(pin[tool]["sha256"].encode())
    tag = f"zsbench-agent:{digest.hexdigest()[:16]}"
    info = {"tag": tag, "task_image": exp.task_image, "codex_config": config, "task_adjustments": json.loads(adjustments)}
    if docker("image", "inspect", tag, check=False).strip() not in ("", "[]"):
        return tag, info
    context = cache / "agent-context" / tag.split(":", 1)[1]
    shutil.rmtree(context, ignore_errors=True)
    context.mkdir(parents=True)
    zeroshot = download(pin["zeroshot"]["url"], pin["zeroshot"]["sha256"], cache / "downloads" / Path(pin["zeroshot"]["url"]).name)
    _extract(zeroshot, pin["zeroshot"]["member"], context / "zeroshot")
    codex = download(pin["codex"]["url"], pin["codex"]["sha256"], cache / "downloads" / Path(pin["codex"]["url"]).name)
    with tarfile.open(codex) as tar:
        tar.extractall(context / "codex-package", filter="data")
    shutil.copy(ROOT / "agent" / "Dockerfile", context / "Dockerfile")
    shutil.copy(ROOT / "agent" / "prepare-task.py", context / "prepare-task.py")
    (context / "codex-config.toml").write_text(config)
    (context / "task-adjustments.json").write_text(adjustments)
    log(f"building {tag} from {exp.task_image}")
    docker("build", "--quiet", "--build-arg", f"TASK_IMAGE={exp.task_image}", "-t", tag, str(context), timeout=3600)
    return tag, info


def _reaper_image(base: str, info: dict[str, Any], cache: Path) -> tuple[str, dict[str, Any]]:
    """The agent image with `codex` run under agent/codex-reaper.c, compiled in the image."""
    digest = hashlib.sha256(base.encode())
    for name in ("reaper.Dockerfile", "codex-reaper.c"):
        digest.update((ROOT / "agent" / name).read_bytes())
    tag = f"{base}-reaper-{digest.hexdigest()[:12]}"
    info = {**info, "tag": tag, "base_agent_image": base, "codex_reaper_sha256": sha256_file(ROOT / "agent" / "codex-reaper.c")}
    if docker("image", "inspect", tag, check=False).strip() not in ("", "[]"):
        return tag, info
    context = cache / "agent-context" / tag.split(":", 1)[1]
    shutil.rmtree(context, ignore_errors=True)
    context.mkdir(parents=True)
    shutil.copy(ROOT / "agent" / "reaper.Dockerfile", context / "Dockerfile")
    shutil.copy(ROOT / "agent" / "codex-reaper.c", context / "codex-reaper.c")
    log(f"building {tag} from {base}")
    docker("build", "--quiet", "--build-arg", f"AGENT_IMAGE={base}", "-t", tag, str(context), timeout=1800)
    return tag, info


def container_env(network: "Network") -> list[str]:
    """``docker run`` environment for an agent container: Codex reaches its API through the allowlist
    proxy; tool commands get no proxy settings (the Codex config withholds them) and so no route out."""
    env = []
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        env += ["-e", f"{name}={network.proxy_url}"]
    for name in ("NO_PROXY", "no_proxy"):
        env += ["-e", f"{name}=localhost,127.0.0.1,::1"]
    return env


# The attempt's container reaches its proxy by this alias on the attempt's own network. A proxy's
# container name (experiment id, attempt label and suffixes) can be longer than a DNS label's 63
# characters, and such a name does not resolve.
PROXY_ALIAS = "egress-proxy"


class Network:
    """A private internal Docker network whose only exit is its own allowlist proxy.

    Every attempt gets one, so concurrent attempts cannot reach each other and each proxy log
    belongs to exactly one attempt.
    """

    def __init__(self, name: str, proxy_image: str, labels: tuple[str, ...] = ()):
        self.name = name
        self.proxy = f"{name}-proxy"
        self.proxy_image = proxy_image
        self.labels = [arg for label in ("zsbench=1", *labels) for arg in ("--label", label)]

    @property
    def proxy_url(self) -> str:
        return f"http://{PROXY_ALIAS}:8888"

    def up(self) -> None:
        self.down()
        docker("network", "create", "--internal", *self.labels, self.name)
        docker("run", "-d", "--name", self.proxy, *self.labels, "--restart", "unless-stopped", "--network", self.name, "--network-alias", PROXY_ALIAS, self.proxy_image)
        docker("network", "connect", "bridge", self.proxy)

    def proxy_log(self) -> str:
        result = run(["docker", "logs", self.proxy], check=False)
        return result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")

    def down(self) -> None:
        docker("rm", "-f", self.proxy, check=False)
        docker("network", "rm", self.name, check=False)
