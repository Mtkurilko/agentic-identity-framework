"""On-disk store: a plain directory (ideally a git repo) of append-only JSONL files."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path

from aif import FORMAT
from aif.events import seal
from aif.templates import AIF_MD, CORE_MD, GITATTRIBUTES, GITIGNORE, PERSONA_MD

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

DEFAULT_CONFIG = {"format": FORMAT, "owner": "the user", "budget": 900, "core_budget": 350}


def default_home() -> Path:
    return Path(os.environ.get("AIF_HOME", "~/.aif")).expanduser()


class Store:
    def __init__(self, home: Path | str | None = None, git: bool | None = None):
        self.home = Path(home).expanduser() if home else default_home()
        self.events_dir = self.home / "events"
        self.git = (os.environ.get("AIF_GIT", "1") != "0") if git is None else git

    # ---- layout -----------------------------------------------------------------

    @property
    def core_path(self) -> Path:
        return self.home / "core.md"

    @property
    def persona_path(self) -> Path:
        return self.home / "persona.md"

    @property
    def capsule_path(self) -> Path:
        return self.home / "capsule.md"

    @property
    def config_path(self) -> Path:
        return self.home / "aif.json"

    def exists(self) -> bool:
        # A fresh clone of a store with no events yet has aif.json but no events/ (git skips empty dirs).
        return self.events_dir.is_dir() or self.config_path.exists()

    def init(self, owner: str | None = None) -> list[str]:
        created = []
        self.events_dir.mkdir(parents=True, exist_ok=True)
        for path, text in ((self.events_dir / ".gitkeep", ""), (self.home / "AIF.md", AIF_MD), (self.core_path, CORE_MD),
                           (self.persona_path, PERSONA_MD),
                           (self.home / ".gitattributes", GITATTRIBUTES), (self.home / ".gitignore", GITIGNORE)):
            if not path.exists():
                path.write_text(text, encoding="utf-8")
                created.append(path.name)
        config = self.config()
        if owner:
            config["owner"] = owner
        if not self.config_path.exists() or owner:
            self.config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            created.append(self.config_path.name)
        if self.git and self._git_available() and not (self.home / ".git").exists():
            self._run_git("init", "-q")
            created.append(".git")
        return created

    def config(self) -> dict:
        config = dict(DEFAULT_CONFIG)
        if self.config_path.exists():
            config.update(json.loads(self.config_path.read_text(encoding="utf-8")))
        return config

    def core(self) -> str:
        return self._hand_written(self.core_path)

    def persona(self) -> str:
        return self._hand_written(self.persona_path)

    @staticmethod
    def _hand_written(path: Path) -> str:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        # HTML comments are authoring hints, not content.
        while "<!--" in text and "-->" in text:
            start = text.index("<!--")
            text = text[:start] + text[text.index("-->", start) + 3:]
        return text.strip()

    # ---- events -----------------------------------------------------------------

    def event_files(self) -> list[Path]:
        return sorted(self.events_dir.glob("*.jsonl")) if self.events_dir.exists() else []

    def signature(self) -> tuple:
        """Cheap change detector for cache invalidation (another process may write)."""
        paths = self.event_files() + [self.core_path]
        return tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in paths if p.exists())

    def read_events(self) -> tuple[list[dict], list[str]]:
        events, problems = [], []
        for path in self.event_files():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                line = line.strip()
                if not line or line.startswith(("<<<<<<<", "=======", ">>>>>>>")):
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError as e:
                    problems.append(f"{path.name}:{lineno}: unreadable line ({e.msg})")
                    continue
                if "id" not in ev:
                    ev = seal(ev)  # hand-written line: accepted, gets its id on read
                events.append(ev)
        return events, problems

    @contextlib.contextmanager
    def lock(self):
        self.home.mkdir(parents=True, exist_ok=True)
        with open(self.home / ".lock", "w") as fh:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def append(self, events: list[dict]) -> list[dict]:
        """Seal and append events. Caller holds the lock."""
        sealed = [seal(ev) for ev in events]
        self.events_dir.mkdir(parents=True, exist_ok=True)
        by_month: dict[str, list[str]] = {}
        for ev in sealed:
            by_month.setdefault(ev["t"][:7], []).append(json.dumps(ev, ensure_ascii=False, sort_keys=True))
        for mon, lines in by_month.items():
            with open(self.events_dir / f"{mon}.jsonl", "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        return sealed

    def write_capsule(self, text: str) -> bool:
        if self.capsule_path.exists() and self.capsule_path.read_text(encoding="utf-8") == text:
            return False
        self.capsule_path.write_text(text, encoding="utf-8")
        return True

    # ---- git --------------------------------------------------------------------

    @staticmethod
    def _git_available() -> bool:
        try:
            subprocess.run(["git", "--version"], capture_output=True, check=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            return False

    def is_repo(self) -> bool:
        return self.git and (self.home / ".git").exists()

    def _run_git(self, *args, check=True) -> subprocess.CompletedProcess:
        env = None
        if args[0] == "commit" and not self._has_identity():
            # Never block on a missing git identity; a configured one always wins.
            env = {"GIT_AUTHOR_NAME": "aif", "GIT_AUTHOR_EMAIL": "aif@localhost",
                   "GIT_COMMITTER_NAME": "aif", "GIT_COMMITTER_EMAIL": "aif@localhost", **os.environ}
        return subprocess.run(["git", "-C", str(self.home), *args], capture_output=True, text=True,
                              env=env, check=check)

    def _has_identity(self) -> bool:
        if not hasattr(self, "_identity"):
            r = subprocess.run(["git", "-C", str(self.home), "config", "user.email"], capture_output=True, text=True)
            self._identity = bool(r.stdout.strip())
        return self._identity

    def commit(self, message: str, all_files: bool = False) -> bool:
        if not self.is_repo():
            return False
        paths = ["-A"] if all_files else ["--", "events", "capsule.md", "aif.json"]
        self._run_git("add", *paths, check=False)
        r = self._run_git("commit", "-q", "--no-verify", "-m", message, check=False)
        return r.returncode == 0

    def history_deletions(self) -> list[str]:
        """Commits that removed or changed lines in the event log (append-only violations)."""
        if not self.is_repo():
            return []
        r = self._run_git("log", "--format=@%h %s", "--numstat", "--", "events", check=False)
        bad, current = [], None
        for line in r.stdout.splitlines():
            if line.startswith("@"):
                current = line[1:]
            elif line.strip():
                added, deleted, _ = line.split("\t", 2)
                if deleted not in ("0", "-"):
                    bad.append(f"{current} (-{deleted} lines)")
        return bad
