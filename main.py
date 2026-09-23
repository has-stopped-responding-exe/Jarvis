"""J.A.R.V.I.S.: a safe local automation and voice-assistant runtime.

J.A.R.V.I.S. discovers launchable applications on the current computer, caches the
result, and accepts spoken open, switch, and close commands. It intentionally has
no URL or web-search fallback.
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import shlex
import subprocess
import sys
import time
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

from jarvis_services import JarvisAutomation

try:
    import psutil
except ImportError:  # A friendly startup message is printed in main().
    psutil = None  # type: ignore[assignment]

try:
    import pyaudio
except ImportError:
    pyaudio = None  # type: ignore[assignment]

try:
    import sounddevice as sd
except ImportError:
    sd = None  # type: ignore[assignment]

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None  # type: ignore[assignment]

try:
    import speech_recognition as sr
except ImportError:
    sr = None  # type: ignore[assignment]

try:
    from rapidfuzz import fuzz, process
except ImportError:
    fuzz = None  # type: ignore[assignment]
    process = None  # type: ignore[assignment]

try:
    from pycaw.pycaw import AudioUtilities
except ImportError:
    AudioUtilities = None  # type: ignore[assignment]


OS_NAME = platform.system()  # Detect once: "Windows", "Linux", or "Darwin".
CACHE_VERSION = 3  # v3 adds Desktop shortcuts and registered Windows App Paths.
CACHE_FILE = Path(__file__).resolve().with_name("app_cache.json")
MATCH_THRESHOLD = 64.0
IMPLICIT_MATCH_THRESHOLD = 82.0
AMBIGUITY_MARGIN = 5.0
AMBIENT_CALIBRATION_SECONDS = 0.45
RECOGNITION_LANGUAGE = os.environ.get("VOICELAUNCH_LANGUAGE", "en-IN")
APP_RECOGNITION_LANGUAGE = os.environ.get(
    "VOICELAUNCH_APP_LANGUAGE", "en-US"
)
MICROPHONE_PREFERENCE = os.environ.get("JARVIS_MICROPHONE", "").strip()
ENHANCE_MIC_AUDIO = os.environ.get("JARVIS_ENHANCE_MIC", "1") == "1"
TTS_VOLUME = 1.0  # pyttsx3 range: 0.0 to 1.0
TTS_RATE = 190
TTS_ENABLED = os.environ.get("JARVIS_TTS", "0") == "1"
MINIMUM_SYSTEM_VOLUME = 0.70
LOG_FILE = Path(__file__).resolve().with_name("jarvis.log")
STARTUP_SCRIPT_NAME = "JARVIS.vbs"

AppList = Dict[str, str]
_tts_engine = None
_background_log = None
_instance_mutex = None


class _SoundDeviceStreamAdapter:
    """Expose sounddevice's blocking stream in SpeechRecognition's format."""

    def __init__(self, raw_stream: object) -> None:
        self.raw_stream = raw_stream

    def read(self, size: int) -> bytes:
        data, overflowed = self.raw_stream.read(size)  # type: ignore[attr-defined]
        if overflowed:
            print("[audio] Warning: microphone input overflowed.")
        return bytes(data)

    def flush(self) -> None:
        """Discard audio accumulated while the previous command was processed."""

        available = int(self.raw_stream.read_available)  # type: ignore[attr-defined]
        while available > 0:
            chunk = min(available, 8192)
            self.raw_stream.read(chunk)  # type: ignore[attr-defined]
            available -= chunk


class SoundDeviceMicrophone(sr.AudioSource if sr is not None else object):
    """SpeechRecognition AudioSource using a selected sounddevice microphone.

    This is the Python 3.14-compatible fallback for platforms where PyAudio has
    no prebuilt wheel. Recognizer.adjust_for_ambient_noise() and
    Recognizer.listen() consume it exactly like sr.Microphone.
    """

    def __init__(
        self,
        sample_rate: Optional[int] = None,
        chunk_size: int = 1024,
        device: Optional[int] = None,
    ):
        if sr is None or sd is None:
            raise RuntimeError("SpeechRecognition and sounddevice are required")
        self.device = device
        if sample_rate is None:
            device_info = sd.query_devices(device=device, kind="input")
            sample_rate = int(device_info["default_samplerate"])
        self.SAMPLE_RATE = sample_rate
        self.SAMPLE_WIDTH = 2  # signed 16-bit PCM
        self.CHUNK = chunk_size
        self.stream = None
        self._raw_stream = None

    def __enter__(self) -> "SoundDeviceMicrophone":
        if self.stream is not None:
            raise RuntimeError("The microphone source is already active")
        self._raw_stream = sd.RawInputStream(
            samplerate=self.SAMPLE_RATE,
            blocksize=self.CHUNK,
            device=self.device,
            channels=1,
            dtype="int16",
        )
        self._raw_stream.start()
        self.stream = _SoundDeviceStreamAdapter(self._raw_stream)
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._raw_stream is not None:
            self._raw_stream.stop()
            self._raw_stream.close()
        self.stream = None
        self._raw_stream = None


def _preferred_microphone_index(names: Iterable[tuple[int, str]]) -> Optional[int]:
    """Return the best input-device index for JARVIS_MICROPHONE."""

    if not MICROPHONE_PREFERENCE:
        return None
    preference = MICROPHONE_PREFERENCE.casefold()
    candidates = [(index, name) for index, name in names if preference in name.casefold()]
    if not candidates:
        print(
            f"[audio] Preferred microphone {MICROPHONE_PREFERENCE!r} was not found; "
            "using the Windows default input."
        )
        return None
    exact = [item for item in candidates if item[1].casefold() == preference]
    selected = (exact or candidates)[0]
    print(f"[audio] Selected microphone: {selected[1]} (device {selected[0]})")
    return selected[0]


def _create_microphone() -> object:
    """Create the configured microphone source for either PortAudio backend."""

    if pyaudio is not None:
        names = list(enumerate(sr.Microphone.list_microphone_names()))
        return sr.Microphone(device_index=_preferred_microphone_index(names))
    if sd is None:
        raise RuntimeError("No supported microphone backend is installed")
    devices = sd.query_devices()
    names = [
        (index, str(device["name"]))
        for index, device in enumerate(devices)
        if int(device["max_input_channels"]) > 0
    ]
    selected = _preferred_microphone_index(names)
    if selected is None and not MICROPHONE_PREFERENCE and OS_NAME == "Windows":
        # sounddevice's global default commonly resolves to legacy MME. Prefer
        # the matching WASAPI endpoint for lower latency and a cleaner capture
        # path while keeping the same physical Windows default microphone.
        try:
            default_index = int(sd.default.device[0])
            default_name = str(devices[default_index]["name"]).casefold()
            host_apis = sd.query_hostapis()
            for index, device in enumerate(devices):
                host_name = str(host_apis[int(device["hostapi"])]["name"])
                if (
                    int(device["max_input_channels"]) > 0
                    and str(device["name"]).casefold() == default_name
                    and "WASAPI" in host_name
                ):
                    sample_rate = int(device["default_samplerate"])
                    sd.check_input_settings(
                        device=index,
                        channels=1,
                        dtype="int16",
                        samplerate=sample_rate,
                    )
                    selected = index
                    print(
                        f"[audio] Using low-latency WASAPI microphone: "
                        f"{device['name']} (device {index})"
                    )
                    break
        except (IndexError, KeyError, TypeError, ValueError, sd.PortAudioError) as exc:
            print(f"[audio] WASAPI selection unavailable; using default input: {exc}")
    return SoundDeviceMicrophone(device=selected)


@dataclass(frozen=True)
class AppMatch:
    """The result of comparing a spoken target with discovered apps."""

    selected: Optional[str]
    alternatives: tuple[str, ...] = ()
    scores: tuple[tuple[str, float], ...] = ()


def _normalise(text: str) -> str:
    """Normalise names while retaining useful words and digits."""

    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _add_app(apps: AppList, name: str, launch_value: str) -> None:
    """Add a usable, non-web app without overwriting a better earlier entry."""

    clean_name = " ".join(name.split()).strip()
    launch_value = launch_value.strip()
    if not clean_name or not launch_value:
        return
    # Reject URL-bearing launchers as well as raw URLs. This prevents Linux
    # desktop entries such as `xdg-open https://...` from entering the catalog.
    if re.search(r"(?:^|[\s=])https?://", launch_value, flags=re.IGNORECASE):
        return
    existing = {_normalise(item): item for item in apps}
    if _normalise(clean_name) not in existing:
        apps[clean_name] = launch_value


def _scan_windows() -> AppList:
    """Scan per-user and machine-wide Start Menu .lnk shortcuts.

    A .lnk is itself a valid launch target on Windows, so it is safer than
    guessing executables from uninstall records. Registry entries with a real
    DisplayIcon executable are added as a secondary source.
    """

    apps: AppList = {}
    program_data = os.environ.get("PROGRAMDATA")
    app_data = os.environ.get("APPDATA")
    roots = []
    if app_data:
        roots.append(Path(app_data) / "Microsoft/Windows/Start Menu/Programs")
    if program_data:
        roots.append(Path(program_data) / "Microsoft/Windows/Start Menu/Programs")
    user_profile = os.environ.get("USERPROFILE")
    public_profile = os.environ.get("PUBLIC")
    one_drive = os.environ.get("OneDrive")
    if user_profile:
        roots.append(Path(user_profile) / "Desktop")
    if public_profile:
        roots.append(Path(public_profile) / "Desktop")
    if one_drive:
        roots.append(Path(one_drive) / "Desktop")

    for root in roots:
        if not root.is_dir():
            continue
        try:
            for shortcut in root.rglob("*.lnk"):
                # Shortcut filename is the display name shown in the Start Menu.
                _add_app(apps, shortcut.stem, str(shortcut.resolve()))
        except OSError as exc:
            print(f"[scan] Could not fully scan {root}: {exc}")

    # Some installed programs do not create Start Menu shortcuts. Include only
    # registry records whose DisplayIcon points to an existing local executable.
    try:
        import winreg

        uninstall_keys = (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        )
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for key_path in uninstall_keys:
                try:
                    key = winreg.OpenKey(hive, key_path)
                except OSError:
                    continue
                with key:
                    for index in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            subkey_name = winreg.EnumKey(key, index)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                name = str(winreg.QueryValueEx(subkey, "DisplayName")[0])
                                icon = str(winreg.QueryValueEx(subkey, "DisplayIcon")[0])
                            executable = _display_icon_executable(icon)
                            if executable and executable.is_file():
                                _add_app(apps, name, str(executable))
                        except OSError:
                            continue
    except (ImportError, OSError) as exc:
        print(f"[scan] Registry scan skipped: {exc}")

    # App Paths is Windows' executable registration mechanism. Programs can be
    # launchable from Run even when they have no Start Menu shortcut or usable
    # uninstall icon, so include every existing executable registered here.
    try:
        import winreg

        app_paths_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                key = winreg.OpenKey(hive, app_paths_key)
            except OSError:
                continue
            with key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        subkey_name = winreg.EnumKey(key, index)
                        with winreg.OpenKey(key, subkey_name) as subkey:
                            executable = Path(str(winreg.QueryValueEx(subkey, None)[0]))
                        if executable.is_file() and executable.suffix.casefold() == ".exe":
                            _add_app(apps, executable.stem, str(executable))
                    except OSError:
                        continue
    except (ImportError, OSError) as exc:
        print(f"[scan] App Paths scan skipped: {exc}")

    # Get-StartApps is Windows' registered launch catalog. Unlike filesystem
    # shortcut scanning, it includes packaged Microsoft Store apps and built-in
    # apps such as Settings, Calculator, Camera, and Notepad. AppIDs are launched
    # through the local shell AppsFolder namespace, never through a website.
    for name, app_id in _scan_windows_start_apps():
        _add_app(apps, name, f"apps-folder:{app_id}")

    return apps


def _scan_windows_start_apps() -> list[tuple[str, str]]:
    """Read local Windows Start app names and AppUserModelIDs."""

    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    command = (
        "$OutputEncoding=[Console]::OutputEncoding="
        "[System.Text.UTF8Encoding]::new();"
        "@(Get-StartApps | Select-Object Name,AppID) | ConvertTo-Json -Compress"
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            check=False,
            creationflags=creation_flags,
        )
        if result.returncode != 0:
            print(f"[scan] Get-StartApps failed: {result.stderr.strip()}")
            return []
        payload = json.loads(result.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"[scan] Windows Start app scan skipped: {exc}")
        return []

    if isinstance(payload, dict):
        payload = [payload]
    discovered = []
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("Name") or "").strip()
        app_id = str(entry.get("AppID") or "").strip()
        if not name or not app_id or _is_web_start_entry(app_id):
            continue
        discovered.append((name, app_id))
    print(f"[scan] Found {len(discovered)} registered Windows Start apps.")
    return discovered


def _is_web_start_entry(app_id: str) -> bool:
    """Reject browser URLs and web documents mixed into Windows Start apps."""

    lowered = app_id.casefold().strip().strip('"')
    if re.search(r"https?://", lowered):
        return True
    path_without_arguments = lowered.split(" ", 1)[0]
    return Path(path_without_arguments).suffix in {
        ".htm",
        ".html",
        ".mht",
        ".mhtml",
        ".url",
    }


def _display_icon_executable(value: str) -> Optional[Path]:
    """Extract an executable path from a Windows DisplayIcon registry value."""

    value = os.path.expandvars(value.strip())
    # DisplayIcon commonly ends in an icon resource index such as ",0".
    value = re.sub(r",\s*-?\d+\s*$", "", value).strip().strip('"')
    candidate = Path(value)
    return candidate if candidate.suffix.casefold() in {".exe", ".com"} else None


def _desktop_entry(path: Path) -> Optional[tuple[str, str]]:
    """Read the unlocalised Name and Exec fields from a Linux .desktop file."""

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[scan] Could not read {path}: {exc}")
        return None

    in_desktop_entry = False
    fields: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_desktop_entry = line == "[Desktop Entry]"
            continue
        if not in_desktop_entry or "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key in {"Name", "Exec", "Type", "Hidden"} and key not in fields:
            fields[key] = value.strip()

    if (
        fields.get("Type", "Application") != "Application"
        or fields.get("Hidden", "false").casefold() == "true"
        or not fields.get("Name")
        or not fields.get("Exec")
    ):
        return None

    command = _clean_desktop_exec(fields["Exec"])
    return (fields["Name"], command) if command else None


def _clean_desktop_exec(command: str) -> str:
    """Remove freedesktop argument field codes from an Exec command."""

    # %% means a literal percent. Other %X values are placeholders for files,
    # URLs, icons, or translated names and do not belong in a plain launch.
    marker = "__VOICELAUNCH_PERCENT__"
    command = command.replace("%%", marker)
    command = re.sub(r"%[a-zA-Z]", "", command)
    return " ".join(command.replace(marker, "%").split())


def _scan_linux() -> AppList:
    """Scan system and user freedesktop .desktop application entries."""

    apps: AppList = {}
    roots = (Path.home() / ".local/share/applications", Path("/usr/share/applications"))
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for desktop_file in root.glob("*.desktop"):
                entry = _desktop_entry(desktop_file)
                if entry:
                    _add_app(apps, entry[0], entry[1])
        except OSError as exc:
            print(f"[scan] Could not fully scan {root}: {exc}")
    return apps


def _scan_macos() -> AppList:
    """Scan machine-wide and per-user .app bundles on macOS."""

    apps: AppList = {}
    for root in (Path.home() / "Applications", Path("/Applications")):
        if not root.is_dir():
            continue
        try:
            # rglob includes apps grouped inside subfolders under /Applications.
            for bundle in root.rglob("*.app"):
                _add_app(apps, bundle.stem, str(bundle.resolve()))
        except OSError as exc:
            print(f"[scan] Could not fully scan {root}: {exc}")
    return apps


def _load_cache() -> Optional[AppList]:
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if payload.get("version") != CACHE_VERSION or payload.get("os") != OS_NAME:
            return None
        apps = payload.get("apps")
        if not isinstance(apps, dict) or not apps:
            return None
        return {str(name): str(command) for name, command in apps.items()}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _save_cache(apps: AppList) -> None:
    payload = {
        "version": CACHE_VERSION,
        "os": OS_NAME,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "apps": dict(sorted(apps.items(), key=lambda item: item[0].casefold())),
    }
    temporary = CACHE_FILE.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, CACHE_FILE)
    except OSError as exc:
        print(f"[cache] Could not save app cache: {exc}")


def scan_installed_apps(force_refresh: bool = False) -> AppList:
    """Return installed app display names mapped to local launch paths/commands."""

    if not force_refresh:
        cached = _load_cache()
        if cached is not None:
            print(f"[cache] Loaded {len(cached)} applications from {CACHE_FILE}")
            return cached

    print(f"[scan] Discovering installed applications on {OS_NAME}...")
    if OS_NAME == "Windows":
        apps = _scan_windows()
    elif OS_NAME == "Linux":
        apps = _scan_linux()
    elif OS_NAME == "Darwin":
        apps = _scan_macos()
    else:
        print(f"[scan] Unsupported operating system: {OS_NAME}")
        return {}

    apps = dict(sorted(apps.items(), key=lambda item: item[0].casefold()))
    if apps:
        _save_cache(apps)
    print(f"[scan] Found {len(apps)} launchable applications.")
    return apps


def speak(text: str) -> None:
    """Print feedback and speak it through the offline pyttsx3 engine."""

    global _tts_engine
    print(f"J.A.R.V.I.S.: {text}")
    if not TTS_ENABLED or pyttsx3 is None:
        return
    try:
        if _tts_engine is None:
            _tts_engine = pyttsx3.init()
            # Maximise VoiceLaunch itself without unexpectedly changing the
            # user's system-wide Windows master volume.
            _tts_engine.setProperty("volume", TTS_VOLUME)
            _tts_engine.setProperty("rate", TTS_RATE)
        _maximise_voice_session_volume()
        _tts_engine.say(text)
        _tts_engine.runAndWait()
        # SAPI may create its Windows audio session only during first playback.
        _maximise_voice_session_volume()
    except Exception as exc:  # Audio drivers vary substantially by platform.
        print(f"[tts] Speech output failed: {exc}")
        _tts_engine = None


def _maximise_voice_session_volume() -> None:
    """Unmute and maximise VoiceLaunch in the Windows per-app volume mixer."""

    if OS_NAME != "Windows" or AudioUtilities is None:
        return
    try:
        for session in AudioUtilities.GetAllSessions():
            process_info = session.Process
            if process_info is None or process_info.pid != os.getpid():
                continue
            session_volume = session.SimpleAudioVolume
            session_volume.SetMute(0, None)
            session_volume.SetMasterVolume(1.0, None)
            print("[tts] VoiceLaunch mixer volume: 100%")
    except Exception as exc:
        print(f"[tts] Could not adjust VoiceLaunch mixer volume: {exc}")


def adjust_system_volume(
    *, minimum: Optional[float] = None, change: Optional[float] = None
) -> Optional[int]:
    """Safely unmute and adjust the default Windows output volume."""

    if OS_NAME != "Windows" or AudioUtilities is None:
        return None
    try:
        device = AudioUtilities.GetSpeakers()
        endpoint = device.EndpointVolume
        current = float(endpoint.GetMasterVolumeLevelScalar())
        if endpoint.GetMute():
            endpoint.SetMute(0, None)
        target = current
        if minimum is not None:
            target = max(target, minimum)
        if change is not None:
            target = target + change
        target = max(0.0, min(1.0, target))
        if abs(target - current) >= 0.005:
            endpoint.SetMasterVolumeLevelScalar(target, None)
        percent = round(target * 100)
        print(f"[audio] Windows speaker volume: {percent}%")
        return percent
    except Exception as exc:
        print(f"[audio] Could not adjust Windows speaker volume: {exc}")
        return None


def _enhance_speech_audio(audio: "sr.AudioData") -> "sr.AudioData":
    """Remove low-frequency rumble and safely normalize 16-bit microphone PCM."""

    if not ENHANCE_MIC_AUDIO or sr is None or audio.sample_width != 2:
        return audio
    samples = array("h")
    samples.frombytes(audio.frame_data)
    if sys.byteorder != "little":
        samples.byteswap()
    if len(samples) < 2:
        return audio

    # A one-pole 80 Hz high-pass removes fan/desk rumble and DC offset without
    # cutting the speech band. Normalize quiet microphones but never amplify
    # more than 3x, which avoids turning room noise into clipping.
    cutoff_hz = 80.0
    dt = 1.0 / float(audio.sample_rate)
    rc = 1.0 / (2.0 * math.pi * cutoff_hz)
    alpha = rc / (rc + dt)
    filtered = array("h")
    previous_input = int(samples[0])
    previous_output = 0.0
    square_sum = 0.0
    peak = 1
    for raw_sample in samples:
        current = int(raw_sample)
        output = alpha * (previous_output + current - previous_input)
        previous_input = current
        previous_output = output
        value = max(-32768, min(32767, int(output)))
        filtered.append(value)
        square_sum += float(value * value)
        peak = max(peak, abs(value))

    rms = math.sqrt(square_sum / len(filtered))
    if rms < 1.0:
        return audio
    gain = min(3.0, 3500.0 / rms, 30000.0 / peak)
    if gain > 1.02:
        for index, value in enumerate(filtered):
            filtered[index] = max(-32768, min(32767, int(value * gain)))
    if sys.byteorder != "little":
        filtered.byteswap()
    print(f"[audio] Voice enhancement: RMS {rms:.0f}, gain {gain:.2f}x")
    return sr.AudioData(filtered.tobytes(), audio.sample_rate, audio.sample_width)


def _app_command_score(text: Optional[str], app_list: Optional[AppList]) -> float:
    """Return the best installed-app score for an explicit spoken command."""

    if not text or not app_list:
        return 100.0
    normalised = _normalise(_strip_wake_word(text))
    if not re.search(
        r"\b(?:open|launch|start|run|close|quit|switch)\b", normalised
    ):
        return 100.0
    _, target = parse_intent(normalised, app_list)
    if not target:
        return 100.0
    candidate = match_app(target, app_list, minimum_score=0.0)
    return candidate.scores[0][1] if candidate.scores else 0.0


def _merge_google_alternatives(primary: object, fallback: object) -> object:
    """Merge unique Google transcript alternatives from two language models."""

    combined: list[dict[str, object]] = []
    seen: set[str] = set()
    for response in (primary, fallback):
        if not isinstance(response, dict):
            continue
        alternatives = response.get("alternative", [])
        if not isinstance(alternatives, list):
            continue
        for item in alternatives:
            if not isinstance(item, dict):
                continue
            transcript = str(item.get("transcript", "")).strip()
            key = transcript.casefold()
            if transcript and key not in seen:
                seen.add(key)
                combined.append(item)
    return {"alternative": combined}


def _select_google_transcript(
    recognition: object, app_list: Optional[AppList]
) -> Optional[str]:
    """Prefer Google's alternative that best matches an installed app.

    Google often returns several plausible transcripts for short brand names.
    Its first result is not always the useful one (for example, "drive flight"
    instead of "Riot Client"). Ordinary conversation still uses the first
    result; app commands are ranked against the live installed-app catalogue.
    """

    if isinstance(recognition, str):
        return recognition.strip() or None
    if not isinstance(recognition, dict):
        return None
    raw_alternatives = recognition.get("alternative", [])
    if not isinstance(raw_alternatives, list):
        return None

    alternatives: list[tuple[str, float, int]] = []
    for index, item in enumerate(raw_alternatives):
        if not isinstance(item, dict):
            continue
        transcript = str(item.get("transcript", "")).strip()
        if not transcript:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        alternatives.append((transcript, confidence, index))
    if not alternatives:
        return None

    first = alternatives[0][0]
    if not app_list:
        return first

    ranked: list[tuple[float, float, int, str]] = []
    for transcript, confidence, index in alternatives:
        normalised = _normalise(_strip_wake_word(transcript))
        if not re.search(
            r"\b(?:open|launch|start|run|close|quit|switch)\b", normalised
        ):
            continue
        _, target = parse_intent(normalised, app_list)
        candidate = match_app(target, app_list, minimum_score=0.0)
        app_score = candidate.scores[0][1] if candidate.scores else 0.0
        ranked.append((app_score, confidence, -index, transcript))

    if not ranked:
        return first
    selected = max(ranked)[3]
    if len(alternatives) > 1:
        print(
            "[audio] Google alternatives: "
            + " | ".join(item[0] for item in alternatives[:5])
        )
        if selected != first:
            print(f"[audio] Selected installed-app interpretation: {selected}")
    return selected


def listen_for_command(
    recognizer: Optional["sr.Recognizer"] = None,
    *,
    audio_source: Optional[object] = None,
    app_list: Optional[AppList] = None,
    timeout: Optional[float] = None,
    phrase_time_limit: float = 7.0,
) -> Optional[str]:
    """Listen with the default microphone and transcribe through Google STT."""

    if sr is None:
        print("[audio] SpeechRecognition is not installed.")
        return None
    recognizer = recognizer or sr.Recognizer()
    try:
        if audio_source is None:
            # Preserve standalone function use by opening a temporary source.
            microphone = _create_microphone()
            backend = "PyAudio" if pyaudio is not None else "sounddevice"
            with microphone as source:
                print(f"[audio] Using {backend} with the default microphone.")
                return listen_for_command(
                    recognizer,
                    audio_source=source,
                    app_list=app_list,
                    timeout=timeout,
                    phrase_time_limit=phrase_time_limit,
                )
        else:
            source = audio_source
            print("Listening...")
            # The same Recognizer survives for the full session, so calibrating
            # on every command only adds delay. Dynamic energy adjustment keeps
            # adapting after this one-time startup calibration.
            if not getattr(recognizer, "_voicelaunch_calibrated", False):
                print("[audio] Calibrating ambient noise once...")
                recognizer.adjust_for_ambient_noise(
                    source, duration=AMBIENT_CALIBRATION_SECONDS
                )
                setattr(recognizer, "_voicelaunch_calibrated", True)
            elif hasattr(source.stream, "flush"):
                source.stream.flush()
            audio = recognizer.listen(
                source, timeout=timeout, phrase_time_limit=phrase_time_limit
            )
        print("[audio] Recognizing speech...")
        audio = _enhance_speech_audio(audio)
        recognition = recognizer.recognize_google(
            audio, language=RECOGNITION_LANGUAGE, show_all=True
        )
        text = _select_google_transcript(recognition, app_list)
        if (
            app_list
            and APP_RECOGNITION_LANGUAGE.casefold()
            != RECOGNITION_LANGUAGE.casefold()
            and _app_command_score(text, app_list) < MATCH_THRESHOLD
        ):
            print(
                "[audio] Retrying unclear app name with "
                f"{APP_RECOGNITION_LANGUAGE} recognition..."
            )
            fallback = recognizer.recognize_google(
                audio,
                language=APP_RECOGNITION_LANGUAGE,
                show_all=True,
            )
            recognition = _merge_google_alternatives(recognition, fallback)
            text = _select_google_transcript(recognition, app_list)
        if not text:
            raise sr.UnknownValueError()
        print(f"[heard] {text}")
        return text
    except sr.WaitTimeoutError:
        print("[audio] No speech detected before timeout.")
    except sr.UnknownValueError:
        # Do not speak here: the microphone can hear that response and create
        # a self-sustaining recognition/TTS feedback loop. A real command
        # failure still receives spoken feedback in the main loop.
        print("[audio] Speech was not understood; listening again.")
    except sr.RequestError as exc:
        speak("The speech recognition service is unavailable right now.")
        print(f"[audio] Google speech recognition error: {exc}")
    except (OSError, AttributeError) as exc:
        speak("I couldn't access the default microphone.")
        print(f"[audio] Microphone error: {exc}")
    except Exception as exc:
        # PortAudio errors differ between the PyAudio and sounddevice backends.
        speak("I couldn't access the default microphone.")
        print(f"[audio] Microphone error ({type(exc).__name__}): {exc}")
    return None


def parse_intent(text: str, app_list: AppList) -> tuple[str, str]:
    """Extract an open/close/switch action and target app from speech."""

    cleaned = _normalise(text)
    action = "open"

    action_patterns = (
        ("close", r"\b(?:close|quit|terminate|exit from)\b"),
        ("switch", r"\b(?:switch to|switch|focus|go to)\b"),
        ("open", r"\b(?:open|launch|start|run)\b"),
    )
    earliest: Optional[tuple[int, str, re.Match[str]]] = None
    for candidate, pattern in action_patterns:
        found = re.search(pattern, cleaned)
        if found and (earliest is None or found.start() < earliest[0]):
            earliest = (found.start(), candidate, found)
    if earliest:
        action = earliest[1]
        action_pattern = next(
            pattern for candidate, pattern in action_patterns if candidate == action
        )
        repeated = list(re.finditer(action_pattern, cleaned))
        # ASR sometimes repeats a partial command ("open set open settings").
        # When the repeated final action has a target after it, trust that tail.
        if len(repeated) > 1 and cleaned[repeated[-1].end() :].strip():
            cleaned = cleaned[repeated[-1].end() :].strip()
        else:
            cleaned = (
                cleaned[: earliest[2].start()] + " " + cleaned[earliest[2].end() :]
            ).strip()

    # Remove conversational padding without removing words from the app name's
    # middle. Leading/trailing "app" is likewise not useful for fuzzy matching.
    cleaned = re.sub(
        r"^(?:(?:hey|please|could you|would you|can you|will you)\s+)*", "", cleaned
    ).strip()
    cleaned = re.sub(r"\s+(?:please|for me|now)$", "", cleaned).strip()
    cleaned = re.sub(r"^(?:the\s+)?(?:app|application)\s+", "", cleaned).strip()
    # Preserve "App" or "Application" when it is genuinely part of an
    # installed display name (for example Intel Rapid Storage Technology
    # Application); otherwise treat it as conversational padding.
    installed_names = {_normalise(name) for name in app_list}
    if _normalise(cleaned) not in installed_names:
        cleaned = re.sub(r"\s+(?:app|application)$", "", cleaned).strip()
    return action, cleaned


def _similarity(query: str, choice: str) -> float:
    if fuzz is not None:
        return float(fuzz.WRatio(query, choice))
    # Keep a standard-library fallback so a missing optional matcher is graceful.
    import difflib

    return difflib.SequenceMatcher(None, query, choice).ratio() * 100.0


def match_app(
    spoken_name: str,
    app_list: AppList,
    minimum_score: float = MATCH_THRESHOLD,
) -> AppMatch:
    """Fuzzy-match a spoken target and flag genuinely ambiguous top matches."""

    query = _normalise(spoken_name)
    if not query or not app_list:
        return AppMatch(None)

    normalised_names = {_normalise(name): name for name in app_list}
    if query in normalised_names:
        name = normalised_names[query]
        return AppMatch(name, scores=((name, 100.0),))

    if process is not None and fuzz is not None:
        raw = process.extract(query, list(app_list), scorer=fuzz.WRatio, limit=5)
        scored = [(str(name), float(score)) for name, score, _ in raw]
    else:
        scored = sorted(
            ((name, _similarity(query, _normalise(name))) for name in app_list),
            key=lambda item: item[1],
            reverse=True,
        )[:5]

    eligible = [(name, score) for name, score in scored if score >= minimum_score]
    if not eligible:
        return AppMatch(None, scores=tuple(scored))

    top_score = eligible[0][1]
    alternatives = tuple(
        name for name, score in eligible if top_score - score <= AMBIGUITY_MARGIN
    )
    if len(alternatives) > 1:
        return AppMatch(None, alternatives=alternatives, scores=tuple(scored))
    return AppMatch(eligible[0][0], scores=tuple(scored))


def _linux_command(command: str) -> list[str]:
    """Convert a desktop Exec string to argv without invoking a shell."""

    parts = shlex.split(command, posix=True)
    if not parts:
        raise ValueError("The desktop entry has an empty Exec command")
    # The desktop spec permits leading NAME=value environment assignments.
    while parts and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", parts[0]):
        parts.pop(0)
    if parts and parts[0] == "env":
        parts.pop(0)
        while parts and (parts[0].startswith("-") or "=" in parts[0]):
            parts.pop(0)
    if not parts:
        raise ValueError("The desktop entry does not contain an executable")
    return parts


def launch_app(app_info: str) -> tuple[bool, str]:
    """Launch one discovered app using only its local path or Exec command."""

    try:
        if OS_NAME == "Windows":
            if app_info.startswith("apps-folder:"):
                app_id = app_info.removeprefix("apps-folder:")
                if _is_web_start_entry(app_id):
                    raise ValueError("web launch entries are not allowed")
                subprocess.Popen(
                    ["explorer.exe", f"shell:AppsFolder\\{app_id}"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                path = Path(app_info)
                if not path.exists():
                    raise FileNotFoundError(path)
                os.startfile(str(path))  # type: ignore[attr-defined]
        elif OS_NAME == "Linux":
            subprocess.Popen(
                _linux_command(app_info),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        elif OS_NAME == "Darwin":
            bundle = Path(app_info)
            if not bundle.is_dir() or bundle.suffix.casefold() != ".app":
                raise FileNotFoundError(bundle)
            subprocess.Popen(
                ["open", "-a", str(bundle)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            raise RuntimeError(f"Unsupported operating system: {OS_NAME}")
        return True, ""
    except (OSError, ValueError, RuntimeError) as exc:
        return False, str(exc)


def _process_aliases(app_name: str, app_info: Optional[str]) -> set[str]:
    aliases = {_normalise(app_name), _normalise(Path(app_name).stem)}
    if app_info:
        if OS_NAME == "Linux":
            try:
                executable = Path(_linux_command(app_info)[0]).name
                aliases.add(_normalise(Path(executable).stem))
            except (ValueError, OSError):
                pass
        elif OS_NAME == "Windows" and Path(app_info).suffix.casefold() == ".lnk":
            # If pywin32 is available, resolve the shortcut so a display name
            # such as "Visual Studio Code" can match a process such as Code.exe.
            try:
                from win32com.client import Dispatch

                shortcut = Dispatch("WScript.Shell").CreateShortCut(app_info)
                target = str(shortcut.Targetpath or "")
                if target:
                    aliases.add(_normalise(Path(target).stem))
            except (ImportError, OSError, AttributeError):
                pass
        elif Path(app_info).suffix.casefold() in {".exe", ".com", ".app"}:
            aliases.add(_normalise(Path(app_info).stem))
    return {alias for alias in aliases if len(alias) >= 2}


def close_app(app_name: str, app_info: Optional[str] = None) -> tuple[bool, str]:
    """Gracefully terminate processes whose executable/title matches the app."""

    if psutil is None:
        return False, "psutil is not installed"
    aliases = _process_aliases(app_name, app_info)
    matches = []
    try:
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                names = {
                    _normalise(Path(str(proc.info.get("name") or "")).stem),
                    _normalise(Path(str(proc.info.get("exe") or "")).stem),
                }
                # Exact aliases avoid terminating an unrelated similarly named app.
                if aliases.intersection(names):
                    matches.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
    except (psutil.Error, OSError) as exc:
        return False, str(exc)

    if not matches:
        return False, "no matching running process was found"

    terminated = 0
    errors = []
    for proc in matches:
        try:
            proc.terminate()
            terminated += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error) as exc:
            errors.append(str(exc))
    if terminated:
        return True, f"sent a terminate request to {terminated} process(es)"
    return False, "; ".join(errors) or "the process could not be terminated"


def _choose_alternative(
    alternatives: Iterable[str],
    recognizer: "sr.Recognizer",
    audio_source: Optional[object] = None,
) -> Optional[str]:
    options = tuple(alternatives)
    joined = " or ".join(options)
    speak(f"Did you mean {joined}?")
    answer = listen_for_command(
        recognizer,
        audio_source=audio_source,
        timeout=7,
        phrase_time_limit=5,
    )
    if not answer:
        return None
    normalised = _normalise(answer)
    # A bare action word is not a choice. ASR previously heard only "open"
    # during clarification and fuzzy-selected an unrelated first option.
    choice_answer = re.sub(
        r"^(?:(?:jarvis|please)\s+)*(?:open|launch|start|run|choose|select)\s*",
        "",
        normalised,
    ).strip()
    if not choice_answer:
        return None
    ordinals = {"first": 0, "one": 0, "second": 1, "two": 1, "third": 2, "three": 2}
    for word, index in ordinals.items():
        if word in choice_answer.split() and index < len(options):
            return options[index]
    scores = sorted(
        ((option, _similarity(choice_answer, _normalise(option))) for option in options),
        key=lambda item: item[1],
        reverse=True,
    )
    if not scores or scores[0][1] < 78:
        return None
    if len(scores) > 1 and scores[0][1] - scores[1][1] < 7:
        return None
    return scores[0][0]


def _missing_dependencies() -> list[str]:
    missing = []
    if sr is None:
        missing.append("SpeechRecognition")
    if pyaudio is None and sd is None:
        missing.append("PyAudio or sounddevice")
    if pyttsx3 is None:
        missing.append("pyttsx3")
    if psutil is None:
        missing.append("psutil")
    return missing


def _configure_background_logging() -> None:
    """Redirect hidden-startup output to a persistent local debug log."""

    global _background_log
    try:
        _background_log = LOG_FILE.open("a", encoding="utf-8", buffering=1)
        sys.stdout = _background_log
        sys.stderr = _background_log
        print(f"\n[startup] J.A.R.V.I.S. started at {time.ctime()}")
    except OSError:
        # A logging failure must not prevent voice operation.
        _background_log = None


def _windows_startup_file() -> Path:
    app_data = os.environ.get("APPDATA")
    if not app_data:
        raise RuntimeError("Windows APPDATA is unavailable")
    return (
        Path(app_data)
        / "Microsoft/Windows/Start Menu/Programs/Startup"
        / STARTUP_SCRIPT_NAME
    )


def install_startup() -> tuple[bool, str]:
    """Install a hidden per-user Windows sign-in launcher."""

    if OS_NAME != "Windows":
        return False, "automatic startup installation is currently supported on Windows"
    try:
        startup_file = _windows_startup_file()
        startup_file.parent.mkdir(parents=True, exist_ok=True)
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        executable = pythonw if pythonw.is_file() else Path(sys.executable)
        command = f'"{executable}" "{Path(__file__).resolve()}" --startup'
        vbs_command = command.replace('"', '""')
        content = (
            'Set shell = CreateObject("WScript.Shell")\n'
            f'shell.Run "{vbs_command}", 0, False\n'
        )
        startup_file.write_text(content, encoding="utf-8")
        return True, f"installed {startup_file}"
    except (OSError, RuntimeError) as exc:
        return False, str(exc)


def remove_startup() -> tuple[bool, str]:
    """Remove the per-user Windows sign-in launcher."""

    if OS_NAME != "Windows":
        return False, "automatic startup removal is currently supported on Windows"
    try:
        startup_file = _windows_startup_file()
        if startup_file.exists():
            startup_file.unlink()
            return True, f"removed {startup_file}"
        return True, "J.A.R.V.I.S. was not registered for automatic startup"
    except (OSError, RuntimeError) as exc:
        return False, str(exc)


def _acquire_single_instance() -> bool:
    """Prevent two Windows copies from listening to the microphone at once."""

    global _instance_mutex
    if OS_NAME != "Windows":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Local\\VoiceLaunchAssistant")
        if not handle:
            return True
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _instance_mutex = handle
    except (AttributeError, OSError):
        return True
    return True


def _strip_wake_word(text: str) -> str:
    """Accept optional Jarvis/VoiceLaunch addressing before a command."""

    return re.sub(
        r"^\s*(?:hey\s+)?(?:jarvis|voice\s*launch|voicelaunch)\b[\s,;:-]*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def _has_wake_word(text: str) -> bool:
    """Return whether speech explicitly addresses the assistant."""

    return bool(
        re.match(
            r"^\s*(?:hey\s+)?(?:jarvis|voice\s*launch|voicelaunch)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _handle_conversation(text: str) -> bool:
    """Handle a small local conversational layer without web fallbacks."""

    normalised = _normalise(text)
    if re.fullmatch(
        r"(?:what(?: s| is) the time|what time is it|tell me the time|current time|time)",
        normalised,
    ):
        current_time = time.strftime("%I:%M %p").lstrip("0")
        speak(f"The time is {current_time}.")
        return True
    if re.fullmatch(
        r"(?:what(?: s| is) the date|what date is it|tell me the date|current date|date)",
        normalised,
    ):
        current_date = time.strftime("%A, %B %d, %Y").replace(" 0", " ")
        speak(f"Today is {current_date}.")
        return True
    if normalised in {"hello", "hi", "hey", "good morning", "good afternoon", "good evening"}:
        speak("Hello. All systems are ready. What would you like me to do?")
        return True
    if normalised in {"how are you", "how are you doing", "are you okay"}:
        speak("All systems are operational. Thank you for asking. How can I help?")
        return True
    if normalised in {
        "i am fine",
        "i am good",
        "i am great",
        "im fine",
        "im good",
        "doing well",
        "good",
        "great",
    }:
        speak("I'm glad to hear that. What would you like me to do?")
        return True
    if normalised in {"thank you", "thanks", "thanks jarvis"}:
        speak("You're welcome. I'm standing by.")
        return True
    if normalised in {"who are you", "what are you"}:
        speak("I'm J.A.R.V.I.S., your local automation assistant.")
        return True
    return False


def _startup_greeting() -> str:
    hour = time.localtime().tm_hour
    if hour < 12:
        period = "morning"
    elif hour < 18:
        period = "afternoon"
    else:
        period = "evening"
    return (
        f"Good {period}. J.A.R.V.I.S. is online. "
        "How are you? What would you like me to do?"
    )


def main() -> None:
    """Run the continuous VoiceLaunch listen/parse/execute loop."""

    if "--ui" in sys.argv:
        from jarvis_ui import run_ui

        run_ui(sys.modules[__name__])
        return
    if "--install-startup" in sys.argv:
        success, message = install_startup()
        print(("Success: " if success else "Error: ") + message)
        return
    if "--remove-startup" in sys.argv:
        success, message = remove_startup()
        print(("Success: " if success else "Error: ") + message)
        return
    if "--startup" in sys.argv:
        _configure_background_logging()

    if OS_NAME not in {"Windows", "Linux", "Darwin"}:
        print(f"J.A.R.V.I.S. does not support {OS_NAME}.")
        return
    if not _acquire_single_instance():
        print("[startup] J.A.R.V.I.S. is already running.")
        return
    missing = _missing_dependencies()
    if missing:
        print("Missing dependencies: " + ", ".join(missing))
        print(
            f"Install them with: {sys.executable} -m pip install "
            "-r requirements.txt"
        )
        return

    apps = scan_installed_apps()
    if not apps:
        speak("I couldn't find any launchable applications on this device.")
        return

    jarvis = JarvisAutomation()
    print(
        "[jarvis] Intelligence provider: "
        f"{jarvis.intelligence.provider or 'none'}; "
        f"Home Assistant configured: {jarvis.home.configured}"
    )
    startup_audit = jarvis.auditor.run()
    print(f"[audit] {startup_audit.message}")
    if startup_audit.data:
        print(f"[audit] Report: {startup_audit.data.get('path')}")

    recognizer = sr.Recognizer()
    # Launcher commands are short. These values reduce the silence wait after
    # the user finishes speaking while retaining enough padding for recognition.
    # Allow natural pauses inside multi-word application names. Values that
    # were too aggressive cut "open Riot" down to "open ri".
    recognizer.pause_threshold = 0.75
    recognizer.non_speaking_duration = 0.4
    recognizer.phrase_threshold = 0.2
    recognizer.dynamic_energy_threshold = True
    recognizer.operation_timeout = 5
    print(f"[startup] Found {len(apps)} installed applications.")
    print(f"[startup] {_startup_greeting()}")

    # Keep one microphone stream open for the entire session. Reopening the
    # PortAudio device for every short command added avoidable local latency.
    microphone = _create_microphone()
    audio_source = microphone.__enter__()
    backend = "PyAudio" if pyaudio is not None else "sounddevice"
    print(f"[audio] Persistent {backend} microphone stream opened.")

    while True:
        text = listen_for_command(
            recognizer, audio_source=audio_source, app_list=apps
        )
        if not text:
            continue
        addressed_to_jarvis = _has_wake_word(text)
        text = _strip_wake_word(text)
        normalised = _normalise(text)

        if not normalised:
            speak("At your service. What would you like me to do?")
            continue

        if normalised in {"exit", "quit", "stop", "stop listening", "goodbye"}:
            speak("Stopping J.A.R.V.I.S. Goodbye.")
            break
        automation_result = jarvis.handle(text)
        if automation_result.handled:
            print(f"[automation] {automation_result.message}")
            speak(automation_result.message)
            continue
        if _handle_conversation(text):
            continue
        if normalised in {"volume up", "increase volume", "speak louder", "talk louder"}:
            percent = adjust_system_volume(change=0.10)
            if percent is None:
                speak("I couldn't change the system volume, but my voice is already at maximum.")
            else:
                speak(f"Speaker volume is now {percent} percent.")
            continue
        if normalised in {"volume down", "decrease volume", "speak quieter", "talk quieter"}:
            percent = adjust_system_volume(change=-0.10)
            if percent is None:
                speak("I couldn't change the system volume.")
            else:
                speak(f"Speaker volume is now {percent} percent.")
            continue
        if normalised in {
            "refresh apps",
            "refresh applications",
            "rescan apps",
            "rescan applications",
        }:
            speak("Refreshing the installed application list.")
            apps = scan_installed_apps(force_refresh=True)
            speak(f"Refresh complete. I found {len(apps)} applications.")
            continue

        action, target = parse_intent(text, apps)
        print(f"[intent] action={action!r}, target={target!r}")
        if not target:
            speak("Please say the name of an application.")
            continue

        explicit_app_action = bool(
            re.search(r"\b(?:open|launch|start|run|close|quit|switch)\b", normalised)
        )
        compact_target = re.sub(r"\s+", "", target)
        installed_names = {_normalise(name) for name in apps}
        if (
            explicit_app_action
            and len(compact_target) < 3
            and _normalise(target) not in installed_names
        ):
            speak("I only heard part of the application name. Please say it again.")
            continue
        # A phrase without an app-action verb may simply be a badly transcribed
        # automation command. Require high confidence so it cannot accidentally
        # launch a loosely similar app (for example, "Rana system" -> Steam).
        result = match_app(
            target,
            apps,
            minimum_score=(
                MATCH_THRESHOLD
                if explicit_app_action
                else IMPLICIT_MATCH_THRESHOLD
                if addressed_to_jarvis
                else 101.0
            ),
        )
        print(f"[match] candidates={result.scores}")
        selected = result.selected
        if result.alternatives:
            selected = _choose_alternative(
                result.alternatives, recognizer, audio_source=audio_source
            )
            if selected is None:
                speak("I couldn't determine which application you meant.")
                continue
        if selected is None:
            if explicit_app_action:
                # A short brand name is easy for speech recognition to mangle.
                # Give it one focused attempt before concluding it is absent.
                speak("I couldn't match that. Please repeat only the application name.")
                retry_text = listen_for_command(
                    recognizer,
                    audio_source=audio_source,
                    app_list=apps,
                    timeout=7,
                    phrase_time_limit=5,
                )
                if retry_text:
                    _, retry_target = parse_intent(
                        _strip_wake_word(retry_text), apps
                    )
                    retry_result = match_app(retry_target, apps)
                    print(f"[match] retry candidates={retry_result.scores}")
                    if retry_result.alternatives:
                        selected = _choose_alternative(
                            retry_result.alternatives,
                            recognizer,
                            audio_source=audio_source,
                        )
                    else:
                        selected = retry_result.selected
                if selected is None:
                    speak("That app isn't installed on this device.")
                    continue
            else:
                answer = jarvis.answer(text)
                print(f"[intelligence] {answer.message}")
                speak(answer.message)
                continue

        app_info = apps[selected]
        print(f"[match] selected={selected!r}, launch_value={app_info!r}")
        if action == "close":
            success, detail = close_app(selected, app_info)
            if success:
                speak(f"Closing {selected} now.")
                print(f"[close] {detail}")
            else:
                speak(f"I couldn't close {selected}.")
                print(f"[close] {detail}")
            continue

        success, error = launch_app(app_info)
        if success:
            verb = "Switching to" if action == "switch" else "Opening"
            speak(f"{verb} {selected} now.")
            print(f"[launch] Successfully invoked {app_info!r}")
        else:
            speak(f"I couldn't launch {selected}.")
            print(f"[launch] Failed: {error}")

    microphone.__exit__(None, None, None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nJ.A.R.V.I.S. stopped.")
