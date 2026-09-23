# Cinematic J.A.R.V.I.S. UI Handoff

## Creative Direction

Build an original tactical AI command surface with a central reactor-style voice core, live dialogue ledger, diagnostic telemetry, and Pulse motion. Avoid Marvel logos, armor silhouettes, direct movie graphics, quotations, and exact voice imitation.

## Required Components

1. Enhanced Canvas core: segmented rotating arcs, radial ticks, crosshair, waveform, and explicit boot/listening/thinking/speaking/alert labels.
2. Live dialogue ledger populated from bounded, known-prefix runtime log events.
3. Telemetry strip for local-control, model-provider, microphone, and app-catalog status.
4. F11 fullscreen toggle with Escape exit.
5. Boot convergence sequence and successful-command pulse; no motion-only semantics.

## Engineering Constraints

- Stay native Tkinter; no new dependency.
- Animation target: 20 FPS; pause/reduce while minimized.
- Backend work remains off the UI thread.
- Preserve all existing safe launch, cleanup, startup, and process-protection boundaries.
- Verify at 1000×640 and the host display's default resolution.

## Palette

- Void: `#03070A`
- Surface: `#071219`
- Reactor cyan: `#56E8FF`
- Signal mint: `#73F7C8`
- Diagnostic amber: `#FFBE55`
- Alert red: `#FF6B76`
- Primary text: `#ECFBFF`
- Muted telemetry: `#71949E`
