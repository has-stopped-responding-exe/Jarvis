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

---

## Interaction & Safe Process Cleanup Addendum

### Multi-Path Trade-Off Matrix

| Path | Description | Safety | Interaction | Maintainability | Automation | Total |
|---|---|---:|---:|---:|---:|---:|
| A — Blind cleanup | Infer “unnecessary” from CPU/RAM and terminate automatically | 1 | 2 | 2 | 5 | 10 |
| B — Approved cleanup | Interactive current-user inventory, persistent allowlist, protected core processes, voice-triggered execution | 5 | 5 | 5 | 4 | **19** |
| C — Confirmation every run | Voice asks for per-process confirmation before every cleanup | 4 | 3 | 3 | 2 | 12 |

Decision: **Path B**. “Unnecessary” is subjective; a persistent user-approved list makes the spoken cleanup command deterministic while preserving full automation after initial setup.

### Defensive Threat Model — Exactly Three Failure Scenarios

1. **Critical process is accidentally allowlisted** — enforce a code-owned protected-name set, current-process/parent exclusions, and current-user ownership before termination.
2. **Process exits or changes between inventory and cleanup** — resolve fresh `psutil.Process` objects at execution time and tolerate `NoSuchProcess`/`AccessDenied` per item.
3. **UI and background listener update/read configuration simultaneously** — write JSON atomically through a temporary sibling file and reload the cleanup allowlist immediately before each voice-triggered cleanup.

### Split-Brain Review

- **Architect:** add a Processes view, grouped memory/instance inventory, persistent approve/remove actions, and Overview quick cleanup.
- **SRE Breaker:** never kill by PID remembered from the UI; never include Windows shell, security, audio, driver, assistant, or Python host processes; terminate gracefully and report failures.
- **Synthesizer:** the user approves a process name once, then “clean unnecessary processes” audits and terminates matching current-user instances automatically with visible results.

### Interface Contracts

```text
ProcessCleaner.inventory() -> list[{
  name: str,
  normalized_name: str,
  instances: int,
  memory_mb: float,
  approved: bool,
  protected: bool
}]

ProcessCleaner.set_approved(process_name: str, approved: bool) -> CommandResult
ProcessCleaner.clean() -> CommandResult {
  data: {audited, terminated, errors, estimated_reclaimed_mb}
}
```

Lifecycle:

```text
inventory refresh -> grouped current-user processes -> approve/remove -> atomic config save
voice cleanup -> reload allowlist -> audit fresh processes -> protected/user checks
              -> graceful terminate -> bounded wait -> report -> UI refresh
```

---

## Cinematic Tactical Interface Addendum

### Intent & Motion Personality

Create an original cinematic AI command surface inspired by advanced fictional tactical systems without copying Marvel marks, suit graphics, dialogue, or actor likeness. Motion personality: **Pulse** — 200–400 ms state changes, rhythmic scanner sweeps, and restrained illumination tied to actual assistant state.

### Multi-Path Trade-Off Matrix

| Path | Description | Fidelity | Performance | Accessibility | Integration | Maintainability | Total |
|---|---|---:|---:|---:|---:|---:|---:|
| A — Cosmetic | Recolor existing panels and add more rings | 2 | 5 | 4 | 5 | 5 | 21 |
| B — Tactical native HUD | Stateful reactor core, radial telemetry, live dialogue feed, boot choreography, full-screen mode | 5 | 4 | 4 | 5 | 4 | **22** |
| C — 3D web shell | WebGL/WebGPU holographic scene with Python bridge | 5 | 2 | 3 | 2 | 2 | 14 |

Decision: **Path B**. It preserves the fast native Python runtime while adding meaningful cinematic behavior connected to real listening, thinking, speaking, and action states.

### Defensive Threat Model — Exactly Three Failure Scenarios

1. **Animation starves the Tk event loop** — reuse canvas items where practical, cap Pulse animation at 20 FPS, suspend decorative work while minimized, and keep all backend work threaded.
2. **Log-derived dialogue displays malformed or secret content** — parse only known `[heard]` and `J.A.R.V.I.S.:` prefixes, bound text length, strip control characters, and never display environment/config values.
3. **Fullscreen HUD traps keyboard users** — bind `F11` to toggle and `Escape` to exit fullscreen, preserve visible focus, and keep every action available through standard buttons.

### Split-Brain Review

- **Architect:** a central reactor core communicates assistant state; a telemetry rail and dialogue ledger provide operational context without card clutter.
- **SRE Breaker:** the interface must remain useful with animation disabled, with an empty log, or while the model provider is offline.
- **Synthesizer:** connect every glow and status change to real system state; decorative motion never implies an action succeeded.

### Strict Interface Contracts

```text
HudState {
  phase: "booting" | "standby" | "listening" | "thinking" | "speaking" | "alert",
  last_user: str <= 180 chars,
  last_assistant: str <= 360 chars,
  last_action: str <= 160 chars,
  provider_online: bool,
  engine_online: bool
}

parse_runtime_tail(text: str, previous: HudState) -> HudState
toggle_fullscreen() -> bool
```

State lifecycle:

```text
booting -> engine probe -> standby|listening
listening -> heard transcript -> thinking
thinking -> assistant/action output -> speaking
speaking -> next listen cycle -> listening
any state -> runtime/provider failure -> alert -> listening|standby after recovery
```

### Visual Tokens

- Reactor cyan: high-luminance cool cyan reserved for active state and focus.
- Diagnostic amber: warnings, elevated system load, and pending transitions.
- Deep graphite: layered near-black fields with subtle blue-green separation.
- Typography: Segoe UI Variable Display + Cascadia Mono, both local and zero-latency.
- Reward moments: boot convergence, command acceptance pulse, successful action sweep.
