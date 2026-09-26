# docs/

Guides and design notes for Animal Tracker. Keep these current — they are the
shared memory across human, Claude and Codex sessions. When you change a rule,
threshold, payload field or endpoint, update the relevant doc in the same commit.

| file | what |
|---|---|
| [Setup_Guide.md](Setup_Guide.md) | from a fresh Mac to notifications: install, `run.sh setup`, zones and travel windows, photos, doctor, scheduled reviews |
| [Training_Guide.md](Training_Guide.md) | the feedback loop that improves the local models from reviewed verdicts; `run.sh train`; Create ML export |
| [Design_Decisions.md](Design_Decisions.md) | ADR-style records of the non-obvious choices and why |
| [Local_Vision_Model_Research.md](Local_Vision_Model_Research.md) | survey of on-device vision options (Apple Vision, DINOv2, Core ML, VLMs) behind the local stack |
| [Detection_Quality.md](Detection_Quality.md) | how detection quality is measured: reviews, audits, per-camera dashboard |
| [Notification_Payload.md](Notification_Payload.md) | the APNs/Pushover payload contract shared by backend and watch app |
| [Dependencies_and_References.md](Dependencies_and_References.md) | what we depend on, what we only borrow ideas from, and what we rejected |

Agent playbooks for common tasks are in [`../.claude/skills/`](../.claude/skills/).

Conventions: Markdown, one topic per file, date any research at the top, link
to code with repo-relative paths. Add a row here when you add a file.
