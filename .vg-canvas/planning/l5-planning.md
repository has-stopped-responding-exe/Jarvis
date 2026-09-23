# J.A.R.V.I.S. Desktop UI — L5 Engineering Plan

## Objective

Build a native Windows control surface for the existing Python voice assistant. The UI must expose assistant state, searchable installed applications, typed commands, microphone/startup controls, system audit telemetry, and a readable activity timeline without replacing or destabilizing the background voice engine.

## Multi-Path Trade-Off Matrix

Scores are 1 (weak) to 5 (strong).

| Path | Description | Performance | Accessibility | Maintainability | Scalability | Visual fidelity | Total |
|---|---|---:|---:|---:|---:|---:|---:|
| A — Simple | Stock `tkinter.ttk` form with tabs and default widgets | 5 | 4 | 5 | 2 | 2 | 18 |
| B — Robust | Native Tkinter shell with custom Canvas visuals, structured controller, worker queue, and reusable themed components | 4 | 4 | 4 | 4 | 5 | **21** |
| C — Performance/expansion | Local web frontend plus Python HTTP/WebSocket service or Electron shell | 3 | 5 | 3 | 5 | 5 | 21 |

### Decision

Choose **Path B**. It produces a proper app with no new runtime dependency, works with the installed Python 3.14/Tk environment, starts quickly, and can call the current local backend directly. Path C adds a server, packaging surface, and port/security lifecycle that are unjustified for a single-device assistant.

## Human Quality Signals

- Typography: `Space Grotesk` character translated to installed Windows-safe Segoe UI Variable, paired with Consolas for telemetry; no network font dependency.
- Composition: asymmetric command-center layout with a strong central voice core, narrow navigation rail, live telemetry, and an operator ledger.
- Physical cue: slightly asymmetric status modules and a circular scanner/orb rather than interchangeable rounded SaaS cards.
- Premium pattern adaptation: semantic named color tokens, intentional state transitions, custom loading/listening animation, and persistent status feedback.

## Defensive Threat Model — Exactly Three Failure Scenarios

1. **Backend command timeout or frozen audit**
   - Risk: scanning apps, running an audit, or starting the voice process blocks Tk's event loop.
   - Mitigation: every backend operation runs in a daemon worker; results enter a `queue.Queue`; the UI drains it with `after(80, ...)`. Buttons enter a bounded busy state and recover on exception.

2. **Malformed or hostile typed input**
   - Risk: unbounded text, shell metacharacters, or URL-like input could become an unsafe launch command.
   - Mitigation: cap input at 160 characters, pass text only through existing intent parsing and installed-app matching, reject URLs, and never use `shell=True` or evaluate arbitrary commands.

3. **Race between background engine and UI controls**
   - Risk: repeated Start clicks create duplicate microphone listeners; Stop targets an unrelated Python process; closing the UI leaves uncertain state.
   - Mitigation: identify processes by normalized executable path plus `main.py --startup` command line, disable actions during transitions, reuse the existing single-instance mutex, and leave the background engine running when the dashboard closes.

## Split-Brain Self-Review

### Pass 1 — Architect

- Separate view state from backend operations.
- Reuse `scan_installed_apps`, `match_app`, `parse_intent`, `launch_app`, `close_app`, `install_startup`, and `JarvisAutomation` rather than duplicating launch logic.
- Keep the dashboard optional: `python main.py --ui`; background startup remains `main.py --startup`.
- Use one root window, one main content frame, and view switching without spawning duplicate windows.

### Pass 2 — SRE Breaker

- App cache may be missing/corrupt; force scan must still restore UI state.
- `psutil` may deny access to individual processes; ignore inaccessible processes and do not report a false crash.
- Log can grow or contain undecodable characters; tail a bounded byte range and decode with replacement.
- DPI scaling, 1366×768 screens, and 200% zoom can compress the app; enforce minimum usable dimensions and collapse secondary content before primary controls.

### Pass 3 — Synthesizer

- Ship a native, dependency-free dashboard with deterministic loading and clear state labels.
- Prioritize three tasks: see whether JARVIS is online, issue a command, and launch/search installed apps.
- Keep decorative animation lightweight and stop it when the window is hidden.
- Validate compile, backend contracts, app launch matching without launching test apps, and visual layout via a rendered screenshot.

## Strict Interface Contracts

### `JarvisDesktopApp`

Input:

```text
root: tkinter.Tk
workspace: absolute pathlib.Path
```

Output/state:

```text
engine_state: "offline" | "starting" | "online" | "stopping" | "error"
voice_state: "idle" | "listening" | "thinking" | "speaking"
active_view: "overview" | "applications" | "activity" | "settings"
apps: dict[str, str]
events: queue.Queue[UiEvent]
```

### `UiEvent`

```text
kind: "state" | "apps" | "command" | "audit" | "error" | "log"
payload: JSON-compatible dict or string
timestamp: local ISO-8601 string
```

### Typed command

Input:

```text
text: UTF-8 string, trimmed, 1..160 characters
```

Output:

```text
CommandResult {
  ok: bool,
  title: str,
  detail: str,
  matched_app?: str
}
```

Rules: URL input is rejected; explicit `open|launch|start|run|close|quit|switch` routes through installed-app matching; automation commands route through `JarvisAutomation`; unmatched content never invokes a shell or browser.

### State Lifecycle

```text
window created
    -> loading
        -> app catalog ready
        -> process status resolved
        -> idle/listening

offline --Start--> starting --process verified--> online/listening
online  --Stop---> stopping --process absent----> offline
any transition --exception---------------------> error --Refresh--> resolved state

typed command: idle -> thinking -> success|error -> idle
refresh apps:  ready -> scanning -> ready|error
```

## Verification Gates

- Python compile for all source modules.
- UI smoke test: instantiate, process event loop, switch views, then close without starting a second assistant.
- Contract test for typed Riot/VALORANT matching with no application launch.
- Confirm background process identification uses exact workspace and arguments.
- Keyboard: Tab traversal, Enter submit, Escape clears command, Ctrl+K focuses search.
- Contrast: cyan/white text on near-black surfaces; all muted text remains readable.
- Motion: animation is optional and honors a reduced-motion configuration flag.
- Runtime files (`jarvis.log`, `app_cache.json`) remain uncommitted.
