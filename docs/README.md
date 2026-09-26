# docs/

Persistent design notes for WinstonTracker. The task board and agent
coordination protocol live in [`../ROADMAP.md`](../ROADMAP.md) — read that first. Keep these current — they are
the shared memory across Claude, Codex, and human sessions.

| file | what |
|---|---|
| [Detection_Quality.md](Detection_Quality.md) | Manual review records and per-camera detection-quality dashboard |
| [Architecture.md](Architecture.md) | perception → tracking → notification pipeline, module responsibilities, data flow example |
| [Design_Decisions.md](Design_Decisions.md) | ADR-style records of the non-obvious choices and why |
| [Ring_API_Research.md](Ring_API_Research.md) | how `python-ring-doorbell` actually works: auth, history, recordings, push, pitfalls |
| [Notification_Payload.md](Notification_Payload.md) | the APNs/Pushover payload contract shared by backend and watch app |
| [Watch_App_Plan.md](Watch_App_Plan.md) | watchOS app scope, screens, complication, notification handling |
| [Dependencies_and_References.md](Dependencies_and_References.md) | GitHub survey: what we depend on, what we run as a sidecar, what we only borrow ideas from, and what we rejected |
| [Session_Detection.md](Session_Detection.md) | session-mode detection: staging contract, verdict schema, the scheduled task prompt, rules for the session (no API key) |
| [Production_Readiness.md](Production_Readiness.md) | punch list from the 2026-09-21 review: what runs, what's verified live, what's left to get to a running system |
| [Camera_Settings_Audit.md](Camera_Settings_Audit.md) | live audit of the 8 cameras' Ring motion settings (People Only mode, zones, frequency, battery), causes of missed dog events, and the `ring-settings` script |

Conventions: Markdown, one topic per file, date any research at the top, link
to code with repo-relative paths. Add a row here when you add a file.
