# Troubleshooting

[Home](../README.md) | [Windows guide](windows.md) | [macOS guide](macos.md)

## Prompts are not approved

1. Check that the Yes, Dev tray or menu-bar icon is present. If it is absent,
   start the app. A running browser or an old log is not proof the tray is alive.
2. Check the displayed state. **Off**, **Observe only**, or a burst/timer pause
   prevents approval. **On** should allow matching prompts.
3. Check the disarm timer. With **Until I turn it off**, inactivity should not
   close the app. A burst question left unanswered stops approval until enabled
   again. **Stop silently** pauses and resumes after about a minute.
4. For Edge, enable **Include Microsoft Edge**. On macOS, also check the
   Accessibility permission for the interpreter that is actually running.
5. Read recent logs. Check their timestamps; a successful approval from yesterday
   does not describe the current process.

Windows version 1.5.0 runs each dialog in a scoped worker. A worker that stops
reporting progress is terminated after five seconds and retried with the native
legacy action first. Startup has a separate ten-second limit. Other dialogs can
proceed during the delay. Read `supervisor.log` for worker recovery records.
Do not assume that a faster poll setting removes a blocked provider call.

If an Allow dialog has a different language or title, matching can fail before
an approval record is written. An empty Observe log is therefore not proof that
there was no dialog. Record the exact title, button text, browser version, app
version, and time. Windows' supervised path checks patterns in both
`watcher_supervisor.py` and `watcher.ps1`; changing only standalone worker
arguments does not update the supervisor's discovery filter.

## App closed or did not start

On Windows, open `%LOCALAPPDATA%\YesDev\lifecycle`. `active.json` points to the
latest session that reached tray readiness. Its `state.json` has the process
identity, current state, and last heartbeat. Check `events.jsonl` for the recorded
start, engine events, exit request, exceptions, and observed process exit code.

- `tray_quit`, `tray_loop_stopped`, and `windows_session_end` describe recorded
  exit paths. A duplicate start can record `duplicate_instance` and code zero.
- `unhandled_exception` includes a traceback. See `stderr.log` and
  `native-fault.log` in that session for additional error output.
- `exit_without_cleanup` means the separate watcher observed a process exit
  without a normal cleanup record. It does not name the external terminator.
- `exit_unrecorded` is marked on the next start if both the tray and its exit
  watcher disappeared without a final record. The last heartbeat bounds the
  known activity; it is not an exact exit time or a proven cause.
- `launches/*.json` records launch handoff and verification failures.
  `%LOCALAPPDATA%\YesDev\bootstrap-error.log` records a failure to load the
  runtime helper itself.

The app does not enable system-wide process auditing. A log entry or exit code
cannot always establish which program closed it. If Windows stopped both the
app and its watcher, some details may be unavailable.

On macOS, check `tray.log` and `yes-dev.log`. Windows' per-session lifecycle
format is not implemented by the macOS source app. See the
[macOS guide](macos.md#known-limitations) for permission and focus issues.

## Logs and settings

| File or folder | Windows | macOS |
|---|---|---|
| Data root | `%LOCALAPPDATA%\YesDev\` | `~/Library/Application Support/YesDev/` |
| Settings | `config.json` | `config.json` |
| Approval records | `yes-dev.log` | `yes-dev.log` |
| Tray events | `tray.log` | `tray.log` |
| Worker recovery | `supervisor.log` | Not used |
| Per-session startup/exit records | `lifecycle/sessions/<id>/` | Not used |
| Launch attempts and latest ready session | `lifecycle/launches/`, `lifecycle/active.json` | Not used |
| Relay diagnostics | Current source: `relay.log`, `relay-runs/<id>/status.json`, `relay-runs/<id>/stderr.log` | `relay.log`, `relay-focus.log`, `relay-status.json` |

The approval and tray logs rotate near 1 MB and retain a previous generation.
Windows supervisor logging also rotates separately. Lifecycle records are
per-session: the event stream has its own rotation, the state has a heartbeat,
and session folders and stderr/native-fault files do not share the old two-log
retention policy. Logs can contain process identities, paths, and exception text;
review them before sharing.

The Windows relay log rotates at 1 MiB with two backups. Its per-run diagnostic
folders are retained separately. The published v1.5.0 Windows app has no relay
controls or relay logs.

## Windows relay does not connect

- Confirm that you run v1.6.0 or newer. The v1.5.0 release does not include
  the Windows relay. [Download and update](windows.md#update-or-switch-installations).
- Enable **On** and **Options > Keep one Chrome connection open**. Pause and
  Observe only stop the relay.
- Point the client at the configured relay address, normally
  `http://127.0.0.1:9333`. Direct connections to Chrome do not get reuse.
- Check that `relay_profile` names the User Data root containing
  `DevToolsActivePort`, and that Chrome remote debugging is enabled.
- If the port is occupied, set another `relay_port` and update the client URL.
  The relay reports the conflict without stopping the other program.
- Read **Open relay log** and the current run's `stderr.log`. A missing
  `websockets` dependency requires installing the current requirements again.

**Listening** means the local endpoint is ready; it does not mean Chrome is
already connected. The first client opens that connection. **Held** means the
Chrome connection is open for reuse. A second concurrent client, browser
restart, or failed cleanup can require another approval.
[Complete setup and limits](windows-relay.md)

## Connection mode and focus

Yes, Dev is for consent-based connections enabled through
`chrome://inspect/#remote-debugging`. Command-line debugging with
`--remote-debugging-port` or `--remote-debugging-pipe` is a different mode.
See [Chrome's debugging-switch change](https://developer.chrome.com/blog/remote-debugging-port).

Both platforms' held-connection options apply to tools using the configured
relay address. A tool connecting directly to Chrome can still cause another
prompt. A second concurrent relay client can also need a separate prompt.
Fast focus is macOS-only; the Windows relay is included from version 1.6.0. See [Windows relay details](windows-relay.md) or
[macOS relay details](macos.md#fast-focus-through-the-local-relay-macos-experimental).

The upstream request for persisted approval,
[#825](https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/825), and the
stacked-prompt issue,
[#1794](https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/1794), are
closed as not planned as checked on October 19, 2026. Those issue states are
historical context, not a guarantee about future Chrome behavior.

## What to include in an issue

Include the app version, source or portable installation, OS and browser
versions, prompt language, selected mode, and exact time. Share the relevant
approval/tray/supervisor excerpt and lifecycle session if available. Describe
whether the app exited, paused, or remained running with prompts pending.
Use private profiles for a reproduction; do not close a working browser session
just to test the app. [Test instructions](../tests/README.md) describe the isolated
checks and the difference between dialog dismissal and a verified CDP connection.
