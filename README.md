# J.A.R.V.I.S.

A Windows background voice assistant for installed-app launching, safe system
maintenance, Home Assistant automations, and optional cloud-model intelligence.

## Security boundaries

- J.A.R.V.I.S. can lock Windows with `LockWorkStation`.
- It cannot unlock Windows or bypass the sign-in screen. Use a Windows password,
  PIN, Windows Hello, or a hardware security key.
- Voice unlocking of Windows and smart locks is deliberately disabled.
- Process cleanup terminates only names explicitly listed in
  `jarvis_config.json` under `cleanup_allowlist`.
- Browser and website fallbacks are not used for app launching.

## NVIDIA Nemotron setup

1. Obtain a trial API key from NVIDIA Build.
2. Copy `.env.example` to `.env`.
3. Replace the placeholder value:

```dotenv
NVIDIA_API_KEY=nvapi-your-real-key
JARVIS_MODEL=nvidia/nemotron-3-super-120b-a12b
```

The hosted NVIDIA endpoint is a rate-limited trial service, not guaranteed
unlimited production hosting.

## Other model providers

OpenAI:

```dotenv
OPENAI_API_KEY=your-key
JARVIS_MODEL=gpt-5.6-terra
```

Any OpenAI-compatible provider:

```dotenv
JARVIS_API_KEY=your-key
JARVIS_BASE_URL=https://provider.example/v1
JARVIS_MODEL=provider/model-name
```

Never paste keys into Python source files or commit `.env`.

## MicYou enhanced microphone

[MicYou](https://github.com/LanRhyme/MicYou) can stream an Android phone's
microphone to Windows and apply noise suppression, echo cancellation,
equalization, automatic gain control, and other DSP. On Windows it routes the
processed audio through VB-CABLE:

1. Install the official MicYou desktop and Android releases.
2. Let MicYou install/detect VB-CABLE, then stream from the phone over Wi-Fi or
   USB.
3. In MicYou, select `CABLE Input (VB-Audio Virtual Cable)` as its output.
4. In games and calling apps, select `CABLE Output (VB-Audio Virtual Cable)` as
   the microphone.
5. To make J.A.R.V.I.S. use the same enhanced feed, add this to `.env`:

```dotenv
JARVIS_MICROPHONE=CABLE Output
```

Restart J.A.R.V.I.S. after changing the setting. If the named device is not
available, J.A.R.V.I.S. logs a warning and safely falls back to the Windows
default microphone.

## Home Assistant

Create a long-lived access token and add:

```dotenv
HOME_ASSISTANT_URL=http://homeassistant.local:8123
HOME_ASSISTANT_TOKEN=your-token
```

Edit `jarvis_config.json` to add friendly aliases for entities, scenes, and
scripts. Follow-up phrases such as “make it brighter” use the last controlled
light during the current session.

## Process cleanup

Open **Processes** in the dashboard and approve only the user applications that
J.A.R.V.I.S. may close. The choice is saved to `cleanup_allowlist`; protected
Windows, security, shell, audio, and J.A.R.V.I.S. host processes cannot be
added. You can also edit the list manually:

```json
{
  "cleanup_allowlist": ["example-background-app.exe"]
}
```

System and unknown processes are never killed automatically. After approval,
say “Jarvis, clean unnecessary processes” or use **Clean approved** in the
dashboard. J.A.R.V.I.S. audits fresh running processes, gracefully terminates
matching current-user instances, and reports the result.

## Installation and startup

```powershell
py -m pip install -r .\requirements.txt
py .\main.py --install-startup
py .\main.py
```

## Desktop command interface

Open the native J.A.R.V.I.S. dashboard:

```powershell
py .\main.py --ui
```

The dashboard provides:

- live voice-engine status and Start/Stop controls;
- an animated listening core and typed command bar;
- searchable access to every locally discovered application;
- system CPU, memory, disk, audit, and activity views;
- Windows startup and microphone-enhancement status.

Keyboard shortcuts: `Ctrl+L` focuses the command bar, `Ctrl+K` opens application
search, `Ctrl+P` opens process cleanup, and `F5` refreshes the installed-app
catalog. Closing the dashboard does not stop the background voice listener.

The assistant starts after user sign-in, when Windows permits microphone access.
It is silent by default. To enable TTS, add `JARVIS_TTS=1` to `.env`.

Disable startup:

```powershell
py .\main.py --remove-startup
```

## Example commands

- “Jarvis, open Settings.”
- “Jarvis, run a system audit.”
- “Jarvis, clean background processes.”
- “Jarvis, audit and clean unnecessary processes.”
- “Jarvis, lock my computer.”
- “Jarvis, turn on the desk light.”
- “Jarvis, activate movie mode.”
- “Jarvis, make it brighter.”
