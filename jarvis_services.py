"""Safe automation, system-maintenance, smart-home, and LLM services for J.A.R.V.I.S."""

from __future__ import annotations

import getpass
import json
import os
import platform
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import psutil

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]

try:
    from rapidfuzz import fuzz, process as fuzzy_process
except ImportError:
    fuzz = None  # type: ignore[assignment]
    fuzzy_process = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parent
AUDIT_DIR = ROOT / "audits"
CONFIG_FILE = ROOT / "jarvis_config.json"
if load_dotenv is not None:
    load_dotenv(ROOT / ".env")


@dataclass
class CommandResult:
    handled: bool
    message: str = ""
    data: Optional[dict[str, Any]] = None


def _normalise(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def load_config() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "cleanup_allowlist": [],
        "home_aliases": {},
        "audit_top_processes": 8,
        "assistant_name": "J.A.R.V.I.S.",
    }
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            defaults.update(loaded)
    except (OSError, json.JSONDecodeError):
        pass
    return defaults


class SystemAuditor:
    """Collect a non-invasive system-health snapshot and save it locally."""

    def __init__(self, top_processes: int = 8) -> None:
        self.top_processes = max(3, min(25, int(top_processes)))

    def run(self) -> CommandResult:
        try:
            memory = psutil.virtual_memory()
            system_drive = os.environ.get("SystemDrive", "C:") + "\\"
            disk = psutil.disk_usage(system_drive)
            battery = psutil.sensors_battery()
            processes = []
            for proc in psutil.process_iter(["pid", "name", "memory_percent"]):
                try:
                    processes.append(
                        {
                            "pid": proc.info["pid"],
                            "name": proc.info.get("name") or "unknown",
                            "memory_percent": round(
                                float(proc.info.get("memory_percent") or 0.0), 2
                            ),
                        }
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            processes.sort(key=lambda item: item["memory_percent"], reverse=True)

            report: dict[str, Any] = {
                "created_at": datetime.now().astimezone().isoformat(),
                "computer": socket.gethostname(),
                "user": getpass.getuser(),
                "os": platform.platform(),
                "boot_time": datetime.fromtimestamp(psutil.boot_time()).isoformat(),
                "cpu_percent": psutil.cpu_percent(interval=0.25),
                "memory": {
                    "percent": memory.percent,
                    "available_gb": round(memory.available / (1024**3), 2),
                    "total_gb": round(memory.total / (1024**3), 2),
                },
                "system_disk": {
                    "percent": disk.percent,
                    "free_gb": round(disk.free / (1024**3), 2),
                    "total_gb": round(disk.total / (1024**3), 2),
                },
                "battery": None
                if battery is None
                else {
                    "percent": battery.percent,
                    "plugged_in": battery.power_plugged,
                },
                "top_memory_processes": processes[: self.top_processes],
            }
            AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            path = AUDIT_DIR / f"audit-{datetime.now():%Y%m%d-%H%M%S}.json"
            path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            message = (
                f"Audit complete. CPU {report['cpu_percent']} percent, memory "
                f"{memory.percent} percent, system disk {disk.percent} percent."
            )
            return CommandResult(True, message, {"path": str(path), "report": report})
        except (OSError, psutil.Error) as exc:
            return CommandResult(True, f"System audit failed: {exc}")


class ProcessCleaner:
    """Terminate only explicitly allowlisted, current-user processes."""

    def __init__(self, allowlist: list[str]) -> None:
        self.allowlist = {_normalise(Path(item).stem) for item in allowlist if item}

    def clean(self) -> CommandResult:
        if not self.allowlist:
            return CommandResult(
                True,
                "No cleanup processes are allowlisted. Nothing was terminated.",
            )
        current_pid = os.getpid()
        current_user = getpass.getuser().casefold()
        candidates = []
        for proc in psutil.process_iter(["pid", "name", "username"]):
            try:
                name = _normalise(Path(str(proc.info.get("name") or "")).stem)
                username = str(proc.info.get("username") or "").casefold()
                if (
                    proc.pid != current_pid
                    and name in self.allowlist
                    and current_user in username
                ):
                    candidates.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        terminated = []
        denied = []
        for proc in candidates:
            try:
                name = proc.name()
                proc.terminate()
                terminated.append(name)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error) as exc:
                denied.append(str(exc))
        message = (
            f"Cleanup complete. Terminated {len(terminated)} allowlisted process"
            f"{'es' if len(terminated) != 1 else ''}."
        )
        if denied:
            message += f" {len(denied)} could not be terminated."
        return CommandResult(True, message, {"terminated": terminated, "errors": denied})


class WindowsSecurity:
    """Supported Windows security actions. Unlocking is deliberately excluded."""

    @staticmethod
    def lock() -> CommandResult:
        if platform.system() != "Windows":
            return CommandResult(True, "Device locking is only configured for Windows.")
        try:
            import ctypes

            if ctypes.windll.user32.LockWorkStation():
                return CommandResult(True, "Device locked.")
            return CommandResult(True, "Windows rejected the lock request.")
        except (AttributeError, OSError) as exc:
            return CommandResult(True, f"Could not lock Windows: {exc}")

    @staticmethod
    def unlock_refusal() -> CommandResult:
        return CommandResult(
            True,
            "Voice unlock is disabled. Sign in with your Windows password, PIN, or security key.",
        )


class HomeAssistantClient:
    """Minimal Home Assistant REST client with in-session entity context."""

    def __init__(self, aliases: dict[str, str]) -> None:
        self.base_url = os.environ.get("HOME_ASSISTANT_URL", "").rstrip("/")
        self.token = os.environ.get("HOME_ASSISTANT_TOKEN", "")
        self.aliases = {_normalise(key): value for key, value in aliases.items()}
        self.last_entity_id: Optional[str] = None
        self._states: list[dict[str, Any]] = []
        self._states_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _request(
        self, method: str, path: str, payload: Optional[dict[str, Any]] = None
    ) -> Any:
        if not self.configured:
            raise RuntimeError(
                "Home Assistant is not configured. Set HOME_ASSISTANT_URL and HOME_ASSISTANT_TOKEN."
            )
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            body = response.read().decode("utf-8")
        return json.loads(body) if body else None

    def states(self, force: bool = False) -> list[dict[str, Any]]:
        if force or not self._states or time.monotonic() - self._states_at > 30:
            value = self._request("GET", "/api/states")
            self._states = value if isinstance(value, list) else []
            self._states_at = time.monotonic()
        return self._states

    def _entity_choices(self) -> dict[str, str]:
        choices = dict(self.aliases)
        for state in self.states():
            entity_id = str(state.get("entity_id") or "")
            friendly = str(state.get("attributes", {}).get("friendly_name") or "")
            if entity_id:
                choices[_normalise(entity_id.replace(".", " ").replace("_", " "))] = entity_id
            if friendly:
                choices[_normalise(friendly)] = entity_id
        return choices

    def resolve(self, target: str) -> Optional[str]:
        normalised = _normalise(target)
        if normalised in {"it", "that", "the light", "the device"}:
            return self.last_entity_id
        choices = self._entity_choices()
        if normalised in choices:
            return choices[normalised]
        if fuzzy_process is None or fuzz is None or not choices:
            return None
        match = fuzzy_process.extractOne(normalised, list(choices), scorer=fuzz.WRatio)
        if match and float(match[1]) >= 72:
            return choices[str(match[0])]
        return None

    def call(self, entity_id: str, service: str, **data: Any) -> CommandResult:
        domain = entity_id.split(".", 1)[0]
        payload = {"entity_id": entity_id, **data}
        try:
            self._request("POST", f"/api/services/{domain}/{service}", payload)
            self.last_entity_id = entity_id
            return CommandResult(True, f"Done. {entity_id} {service.replace('_', ' ')}.")
        except (RuntimeError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            return CommandResult(True, f"Home automation failed: {exc}")

    def control(self, target: str, action: str) -> CommandResult:
        try:
            entity_id = self.resolve(target)
        except (RuntimeError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            return CommandResult(True, f"Home automation failed: {exc}")
        if not entity_id:
            return CommandResult(True, f"I could not identify the home device '{target}'.")
        domain = entity_id.split(".", 1)[0]
        if action == "unlock" or (domain == "lock" and action == "open"):
            return CommandResult(
                True,
                "Voice unlocking is disabled. Use the lock's authenticated app or keypad.",
            )
        service_map = {
            "on": "turn_on",
            "off": "turn_off",
            "lock": "lock",
            "open": "open_cover",
            "close": "close_cover" if domain == "cover" else "turn_off",
        }
        service = service_map.get(action)
        if domain == "lock" and action == "lock":
            service = "lock"
        if not service:
            return CommandResult(True, f"Unsupported action '{action}'.")
        return self.call(entity_id, service)

    def adjust_last_light(self, delta: int) -> CommandResult:
        if not self.last_entity_id or not self.last_entity_id.startswith("light."):
            return CommandResult(True, "No light is currently in context.")
        try:
            state = next(
                item
                for item in self.states(force=True)
                if item.get("entity_id") == self.last_entity_id
            )
            brightness = int(state.get("attributes", {}).get("brightness") or 128)
            percent = max(1, min(100, round(brightness / 255 * 100) + delta))
            return self.call(self.last_entity_id, "turn_on", brightness_pct=percent)
        except (StopIteration, RuntimeError, OSError, urllib.error.URLError) as exc:
            return CommandResult(True, f"Could not adjust the light: {exc}")


class IntelligenceClient:
    """Provider-neutral OpenAI-compatible chat client with session memory."""

    SYSTEM_PROMPT = (
        "You are J.A.R.V.I.S., a precise, concise, subtly witty AI butler. "
        "Never claim an automation happened unless the local automation layer confirms it. "
        "Do not provide instructions for bypassing device authentication or security."
    )

    def __init__(self) -> None:
        self.history: list[dict[str, str]] = []
        self.provider = "none"
        self.model = ""
        self.client = None
        if OpenAI is None:
            return

        custom_key = os.environ.get("JARVIS_API_KEY")
        custom_url = os.environ.get("JARVIS_BASE_URL")
        custom_model = os.environ.get("JARVIS_MODEL")
        nvidia_key = os.environ.get("NVIDIA_API_KEY")
        openai_key = os.environ.get("OPENAI_API_KEY")
        try:
            if custom_key and custom_url and custom_model:
                self.provider = "custom"
                self.model = custom_model
                self.client = OpenAI(api_key=custom_key, base_url=custom_url)
            elif nvidia_key:
                self.provider = "nvidia"
                self.model = custom_model or "nvidia/nemotron-3-super-120b-a12b"
                self.client = OpenAI(
                    api_key=nvidia_key,
                    base_url="https://integrate.api.nvidia.com/v1",
                )
            elif openai_key:
                self.provider = "openai"
                self.model = custom_model or "gpt-5.6-terra"
                self.client = OpenAI(api_key=openai_key)
        except Exception:
            self.client = None
            self.provider = "none"

    @property
    def configured(self) -> bool:
        return self.client is not None and bool(self.model)

    def ask(self, prompt: str) -> CommandResult:
        if not self.configured:
            return CommandResult(
                True,
                "No intelligence API is configured. Set NVIDIA_API_KEY, OPENAI_API_KEY, or the JARVIS provider variables.",
            )
        self.history.append({"role": "user", "content": prompt})
        self.history = self.history[-20:]
        try:
            request: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    *self.history,
                ],
                "max_tokens": 500,
            }
            if self.provider == "nvidia":
                request.update(
                    {
                        "temperature": 1.0,
                        "top_p": 0.95,
                        "extra_body": {
                            "chat_template_kwargs": {"enable_thinking": False}
                        },
                    }
                )
            completion = self.client.chat.completions.create(**request)
            message = completion.choices[0].message.content or "No response returned."
            self.history.append({"role": "assistant", "content": message})
            self.history = self.history[-20:]
            return CommandResult(True, message, {"provider": self.provider, "model": self.model})
        except Exception as exc:
            return CommandResult(True, f"Intelligence provider error: {exc}")


class JarvisAutomation:
    """Route deterministic local and smart-home commands before any LLM call."""

    def __init__(self) -> None:
        config = load_config()
        self.auditor = SystemAuditor(config.get("audit_top_processes", 8))
        self.cleaner = ProcessCleaner(config.get("cleanup_allowlist", []))
        self.security = WindowsSecurity()
        self.home = HomeAssistantClient(config.get("home_aliases", {}))
        self.intelligence = IntelligenceClient()

    def handle(self, text: str) -> CommandResult:
        command = _normalise(text)
        audit_command = bool(
            re.search(
                r"\b(?:system audit|audit system|system health|system status)\b",
                command,
            )
            or re.fullmatch(
                r"(?:please )?(?:run a|run|rana|haryana|rna) system(?: audit)?(?: please)?",
                command,
            )
        )
        if audit_command:
            return self.auditor.run()
        if re.search(r"\b(?:clean processes|process cleanup|clean background processes)\b", command):
            return self.cleaner.clean()
        if re.fullmatch(r"(?:please )?(?:lock|secure) (?:my )?(?:computer|device|pc|windows)", command):
            return self.security.lock()
        if re.fullmatch(r"(?:please )?unlock (?:my )?(?:computer|device|pc|windows)", command):
            return self.security.unlock_refusal()

        match = re.fullmatch(r"(?:turn|switch) (on|off) (.+)", command)
        if match:
            return self.home.control(match.group(2), match.group(1))
        match = re.fullmatch(r"(lock|unlock|open|close) (.+)", command)
        if match:
            action, target = match.groups()
            home_target = bool(
                re.search(
                    r"\b(?:door|lock|garage|blind|blinds|curtain|curtains|shade|cover|gate)\b",
                    target,
                )
            )
            if action in {"lock", "unlock"} or home_target:
                return self.home.control(target, action)
        if command in {"make it brighter", "brighter"}:
            return self.home.adjust_last_light(10)
        if command in {"make it dimmer", "dimmer", "make it darker"}:
            return self.home.adjust_last_light(-10)
        match = re.fullmatch(r"(?:run|activate) (.+?)(?: routine| scene| automation)?", command)
        if match and self.home.configured:
            return self.home.control(match.group(1), "on")
        return CommandResult(False)

    def answer(self, text: str) -> CommandResult:
        return self.intelligence.ask(text)
