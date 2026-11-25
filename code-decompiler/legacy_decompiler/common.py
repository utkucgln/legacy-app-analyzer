"""Shared primitives: fact model, writers, masking, local and docker tool runners, text helpers."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("legacy_decompiler")

FACT_TYPES = (
    "screen", "control", "rule", "entity", "relationship", "code_value", "integration",
    "job", "config", "role", "db_logic", "email", "usage", "dead_feature", "finding",
)
CONFIDENCE = ("Confirmed", "Probable", "To verify")


@dataclass
class Fact:
    type: str
    statement: str
    keys: list[str]
    sources: list[dict]
    confidence: str = "Probable"
    conflicts: list[str] = field(default_factory=list)
    business_module: str = ""
    attrs: dict = field(default_factory=dict)
    fact_id: str = ""

    def __post_init__(self) -> None:
        if self.type not in FACT_TYPES:
            raise ValueError(f"unknown fact type {self.type}")
        if self.confidence not in CONFIDENCE:
            raise ValueError(f"unknown confidence {self.confidence}")
        if not self.fact_id:
            digest = hashlib.sha1(
                "|".join([self.type, self.statement, *sorted(self.keys)]).encode("utf-8")
            ).hexdigest()[:10]
            pillar = self.sources[0]["pillar"] if self.sources else "x"
            self.fact_id = f"{pillar[:2].upper()}-{self.type}-{digest}"


def source(pillar: str, artifact: str, locator: str = "") -> dict:
    return {"pillar": pillar, "artifact": artifact, "locator": locator}


class FactSink:
    def __init__(self, pillar: str) -> None:
        self.pillar = pillar
        self.facts: dict[str, Fact] = {}

    def add(self, type_: str, statement: str, keys: Iterable[str], artifact: str,
            locator: str = "", confidence: str = "Probable", **attrs: Any) -> Fact:
        fact = Fact(type_, statement, [k for k in dict.fromkeys(keys) if k],
                    [source(self.pillar, artifact, locator)], confidence, attrs=attrs)
        existing = self.facts.get(fact.fact_id)
        if existing:
            if fact.sources[0] not in existing.sources:
                existing.sources.append(fact.sources[0])
            return existing
        self.facts[fact.fact_id] = fact
        return fact

    def write(self, out_dir: Path) -> Path:
        path = out_dir / "facts.json"
        write_json(path, [asdict(f) for f in self.facts.values()])
        log.info("%s: %d facts -> %s", self.pillar, len(self.facts), path)
        return path


def load_facts(path: Path) -> list[Fact]:
    if not path.exists():
        return []
    return [Fact(**d) for d in json.loads(path.read_text(encoding="utf-8"))]


# ---------------------------------------------------------------- writers

def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def write_csv(path: Path, rows: list[dict], columns: list[str] | None = None) -> None:
    ensure_dir(path.parent)
    if columns is None:
        columns = list(dict.fromkeys(k for r in rows for k in r)) if rows else []
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _cell(row.get(k)) for k in columns})


def _cell(value: Any) -> Any:
    if isinstance(value, (list, tuple, set)):
        return "; ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- masking

SECRET_NAME = re.compile(
    r"(pass(word|wd|phrase)?|pwd|secret|token|api[_.-]?key|apikey|credential|private[_.-]?key|auth[_.-]?key|signing[_.-]?key|hash|salt)",
    re.I,
)
PII_NAME = re.compile(
    r"(name|email|e_mail|phone|mobile|msisdn|address|birth|dob|national|passport|iban|card|pan\b|identity|id_?card|civil_?id|ssn|tax_?id)",
    re.I,
)
CARD_RE = re.compile(r"(?<!\d)(\d{6})\d{3,9}(\d{4})(?!\d)")


def mask(value: Any) -> str:
    text = "" if value is None else str(value)
    if not text:
        return ""
    return f"{text[:2]}***({len(text)})"


def mask_cards(text: str) -> str:
    return CARD_RE.sub(lambda m: f"{m.group(1)}******{m.group(2)}", text)


def shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = {c: text.count(c) for c in set(text)}
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def looks_secret_value(text: str) -> bool:
    return len(text) >= 16 and " " not in text and shannon_entropy(text) > 3.5 and any(c.isdigit() for c in text)


# ---------------------------------------------------------------- text helpers

def split_words(name: str) -> list[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", name) if w]


STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "are", "was", "has", "not", "per", "all",
    "class", "method", "field", "value", "values", "table", "column", "com", "org", "java", "action",
    "form", "data", "code", "list", "view", "page", "get", "set", "new", "app", "impl", "dao", "service",
}


def stem_tokens(text: str) -> set[str]:
    return {w[:5] for w in split_words(text) if len(w) > 2 and w not in STOPWORDS and not w.isdigit()}


def numbers_in(text: str) -> set[str]:
    return {n.rstrip("0").rstrip(".") if "." in n else n for n in re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", text)}


def shape_of(value: str) -> str:
    out = []
    for ch in value:
        if ch.isdigit():
            out.append("9")
        elif ch.isalpha():
            out.append("A" if ch.isupper() else "a")
        else:
            out.append(ch)
    return "".join(out)


def shape_regex(shape: str) -> str:
    parts = re.findall(r"9+|A+|a+|[^9Aa]+", shape)
    rx = []
    for p in parts:
        cls = {"9": "[0-9]", "A": "[A-Z]", "a": "[a-z]"}.get(p[0])
        if cls:
            rx.append(cls + (f"{{{len(p)}}}" if len(p) > 1 else ""))
        else:
            rx.append(re.escape(p))
    return "^" + "".join(rx) + "$"


def norm_path(path: str, context_path: str = "") -> str:
    """Normalise a URL path to an application-relative feature key (no context, session id, query, extension .do)."""
    path = re.sub(r";jsessionid=[^?/#]*", "", path, flags=re.I).split("?")[0].split("#")[0]
    if context_path and path.startswith(context_path.rstrip("/") + "/"):
        path = path[len(context_path.rstrip("/")):]
    path = re.sub(r"\.do$", "", path)
    return path or "/"


# ---------------------------------------------------------------- tool runners

class ToolError(RuntimeError):
    pass


class DockerError(ToolError):
    pass


def run_tool(cmd: list[str], timeout: int = 3600, ok_codes: tuple[int, ...] = (0,), log_file: Path | None = None,
             cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a local executable with an argument list (no shell). Output goes to log_file when given, else is captured."""
    log.debug("run %s", " ".join(cmd)[:300])
    if log_file:
        with log_file.open("w", encoding="utf-8", errors="replace") as fh:
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout, cwd=cwd, check=False)
    else:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd, encoding="utf-8", errors="replace", check=False)
    if proc.returncode not in ok_codes:
        detail = log_file.read_text(encoding="utf-8", errors="replace")[-1500:] if log_file else (proc.stderr or proc.stdout or "")[-1500:]
        raise ToolError(f"{Path(cmd[0]).name} exited {proc.returncode}: {detail}")
    return proc


def java_major(exe: str) -> int | None:
    """Java feature version of a java/javac executable (8 for 1.8.x, 17 for 17.x); None if it cannot be determined."""
    try:
        proc = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', proc.stderr + proc.stdout)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2)) if major == 1 and m.group(2) else major


def jdk_tool(home: str | None, tool: str) -> str:
    """Path of a JDK executable: the configured JDK home, then JAVA_HOME, then PATH."""
    exe = tool + (".exe" if os.name == "nt" else "")
    for candidate in (home, os.environ.get("JAVA_HOME")):
        if candidate:
            path = Path(candidate) / "bin" / exe
            if path.exists():
                return str(path)
            if candidate == home:
                raise ToolError(f"{path} not found (check jdk settings in the config)")
    found = shutil.which(tool)
    if not found:
        raise ToolError(f"{tool} not found: set jdk.home in the config, JAVA_HOME, or add the JDK bin folder to PATH")
    return found


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def docker_run(image: str, args: list[str], mounts: list[tuple[Path | str, str, bool]] = (),
               env: dict[str, str] | None = None, pass_env: list[str] = (), network: str | None = None,
               entrypoint: str | None = None, workdir: str | None = None, timeout: int = 3600,
               platform: str | None = None, ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    cmd = ["docker", "run", "--rm"]
    if platform:
        cmd += ["--platform", platform]
    if network:
        cmd += ["--network", network]
    if entrypoint is not None:
        cmd += ["--entrypoint", entrypoint]
    if workdir:
        cmd += ["-w", workdir]
    for host, target, readonly in mounts:
        spec = f"type=bind,source={Path(host).resolve()},target={target}"
        cmd += ["--mount", spec + (",readonly" if readonly else "")]
    for key, value in (env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    for key in pass_env:
        cmd += ["-e", key]  # value taken from this process environment, never on the command line
    cmd += [image, *args]
    log.debug("docker run %s %s", image, " ".join(args)[:300])
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env={**os.environ},
                          encoding="utf-8", errors="replace")
    log.debug("docker %s finished in %.1fs rc=%s", image, time.monotonic() - started, proc.returncode)
    if proc.returncode not in ok_codes:
        raise DockerError(f"{image} exited {proc.returncode}: {(proc.stderr or proc.stdout)[-1500:]}")
    return proc


class Timer:
    def __init__(self) -> None:
        self.started = time.monotonic()

    @property
    def seconds(self) -> float:
        return round(time.monotonic() - self.started, 1)
