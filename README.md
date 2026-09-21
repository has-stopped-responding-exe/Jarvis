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

Add only disposable user applications to `cleanup_allowlist`:

```json
{
  "cleanup_allowlist": ["example-background-app.exe"]
}
```

System and unknown processes are never killed automatically.

## Installation and startup

```powershell
py -m pip install -r .\requirements.txt
py .\main.py --install-startup
py .\main.py
```

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
- “Jarvis, lock my computer.”
- “Jarvis, turn on the desk light.”
- “Jarvis, activate movie mode.”
- “Jarvis, make it brighter.”
