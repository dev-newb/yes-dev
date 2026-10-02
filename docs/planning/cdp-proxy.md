# Plan: a local CDP proxy, so Chrome never prompts after the first connection

> Planning document for a persistent, multiplexed proxy. The separate thin relay
> (`cdp_relay.py`, `relay_mac.py`) opens one upstream per client and implements
> an early-focus handshake; it does not implement the shared socket described
> here. This plan exists because the only way to
> stop Chrome taking focus for a remote-debugging consent prompt, without
> modifying Chrome, is to stop the prompt from happening, and this is how.

## The problem it solves

Chrome's consent sheet is per connection. Every CDP client that opens the
browser websocket gets its own prompt, and every prompt activates Chrome's
window before the sheet is built (`DevToolsConnectionDialog` calls
`browser->GetWindow()->Activate()`). macOS honours that activation; nothing
outside Chrome can veto it. The quiet-focus helper shrinks the interruption to a
blink, but it is still an interruption, and keystrokes
inside that window reach Chrome.

The real Chrome 154 comparison on 2026-10-01 measured 434.5 ms median for the
normal guard and 72.5 ms for the early-signal prototype. The previous roughly
15 ms number came from a stand-in without Chrome's consent-sheet animation.

A proxy removes the prompt instead. Chrome prompts once, for the proxy's own
connection, at launch. Every agent then connects to the proxy, which carries
their traffic over that one approved socket. Chrome never sees a second
connection and never prompts again until it is relaunched.

## What stays and what changes

Stays: the installed Chrome, the main profile with its logins, approval mode
as configured through `chrome://inspect`, and Yes, Dev's existing engine, which
approves the one prompt per launch (or the user does, by hand, once).

Changes: agents connect to the proxy's address instead of Chrome's. For
chrome-devtools-mcp that is `--browserUrl http://127.0.0.1:<proxy port>`; for
Puppeteer, `browserURL` or `browserWSEndpoint` pointing at the proxy; for
Playwright, `connectOverCDP` with the proxy's URL. Nothing else in those tools
changes.

## How Chrome behaves, as far as we know

Known, from the Chromium source at the 154 revision and from live runs:

- In approval mode Chrome writes `DevToolsActivePort` (port on line one, the
  browser endpoint path on line two). Only websocket requests to the browser
  endpoint are accepted; page endpoints get 403.
- An approved websocket stays open indefinitely. While any connection is open,
  Chrome shows the "being controlled by automated software" infobar in every
  window; it goes when the last connection closes.
- Whether the HTTP endpoints (`/json/version`, `/json/list`, `/json/new`) work in
  approval mode is **not established**. One observation had `/json/version`
  returning 404 before approval. The proxy should not depend on them: it can
  answer `/json/version` itself and build `/json/list` from `Target.getTargets`
  over the upstream socket.

To verify before building: hold one approved browser socket open for hours
across sleep and wake, and confirm Chrome keeps it; confirm `Target.getTargets`
and `Target.createTarget` work over it; find out whether `/json/*` answer over
HTTP once a socket is approved.

## Architecture

```
agent A ──ws──┐
agent B ──ws──┤  proxy (127.0.0.1:9333)  ──one approved ws──▶  Chrome browser endpoint
agent C ──ws──┘   HTTP: /json/version, /json/list, /json/new, /json/close
```

The proxy is one process, started and supervised by the tray like the engine
and the clouds. It:

1. Watches `DevToolsActivePort` under the profile directory, so it follows
   Chrome across relaunches.
2. Opens the upstream browser socket on demand, the first time a client
   arrives, or eagerly at Chrome launch if configured. That is the one prompt,
   which the engine approves.
3. Listens on a fixed local port and serves the HTTP discovery endpoints agents
   expect, with every websocket URL rewritten to point at itself.
4. Accepts client websockets at the browser path and at per-target paths, and
   multiplexes them over the upstream socket.

## The hard part: multiplexing

CDP was designed for one client per browser connection. Three things need
virtualising per client.

**Message ids.** Each client numbers its own commands from 1. The proxy maps
`(client, id)` to a unique upstream id and maps the response back.

**Sessions.** Flattened mode (`Target.attachToTarget` with `flatten: true`,
which Puppeteer and Playwright both use) tags every message with a
`sessionId`. The proxy records which client attached which session and routes
session-scoped events to that client only. When a client disconnects, the proxy
sends `Target.detachFromTarget` for each of its sessions.

**Browser-wide state.** These are set per upstream connection, and two clients
can disagree:

- `Target.setDiscoverTargets`: enable upstream once any client asks; fan out
  `targetCreated`/`targetDestroyed`/`targetInfoChanged` to clients that asked.
- `Target.setAutoAttach` with `waitForDebuggerOnStart`: the real conflict.
  Puppeteer enables it on the browser session so that new pages pause until it
  has set up interception. If two clients both have it, each new target is
  attached to both, both get a session, and whichever sends
  `Runtime.runIfWaitingForDebugger` first resumes it for both. The plan is
  **ownership**: a new target is auto-attached only to the client that created
  it (`Target.createTarget` by that client, or a target whose opener belongs to
  that client), and clients can still attach explicitly to anything they
  discover. This matches how a human would expect separate agents to behave and
  avoids two debuggers on one page.
- `Browser.close`, `Target.closeTarget` on targets you do not own,
  `Browser.setDownloadBehavior` and similar: refuse or scope. `Browser.close`
  from a client closes that client's targets and connection, never the browser.
- `Target.createBrowserContext`: allowed; the context is owned by the creator
  and disposed when it disconnects, unless it asked to keep it.

**Events without a session** (`Target.*` browser-level events) go to every
client that enabled discovery; everything else is session-scoped.

## Phases

1. **Pass-through, one client at a time.** *Built, as the relay's `hold`
   option (`HeldConnection` in `cdp_relay.py`, "Keep one Chrome connection
   open" in Settings).* The proxy holds the upstream socket open and lends it
   to one client at a time. Two refinements over the plan above: message ids
   are renumbered per client and stale replies and events dropped, and a client
   arriving while the connection is lent gets its own upstream connection
   instead of waiting or being refused. On release the relay undoes
   browser-level settings, detaches sessions, disposes the contexts Chrome
   itself would, and retires the connection if any of that fails. Run live
   against Chrome 154 with Playwright and Puppeteer on 2026-10-02, which found
   two bugs (auto-attach reset refused without `flatten`; `Browser.close` from a
   concurrent client) that are now fixed and pinned by tests.
2. **Concurrent clients with ownership.** Id mapping, session routing, the
   ownership rule for auto-attach, discovery fan-out, per-client cleanup.
   The bulk of the work. Needs a test matrix, below.
3. **Edges.** Chrome relaunch mid-session (clients get a clean close and
   reconnect), sleep/wake, the infobar, `Target.exposeDevToolsProtocol`,
   service-worker and worker targets, Playwright's `connectOverCDP` specifics.

## Test matrix

Each run holds the real tool against the proxy and checks the agent can do its
normal job while Chrome shows no prompt and the frontmost app never changes
(WindowServer's log is the judge, as in the quiet-focus measurement):

| client | solo | two at once | one leaves mid-task |
|---|---|---|---|
| chrome-devtools-mcp | | | |
| Puppeteer (`connect`) | | | |
| Playwright (`connectOverCDP`) | | | |
| raw websocket (the round-two probe) | | | |

Plus: Chrome relaunched while clients are attached; a client that opens a new
tab while another is driving a different tab; a client that crashes without
detaching.

## Effort and risk

Phase 1: about a day. Phase 2: one to two weeks, most of it in the test
matrix, because every CDP consumer has its own assumptions and the failures are
subtle (a page that never resumes, an event delivered to the wrong agent).
Phase 3: ongoing as tools change.

Risks: Chrome tightening approval mode further in a future release; a tool that
hard-codes the port from `DevToolsActivePort`; a client that needs browser-wide
auto-attach to function and cannot live with ownership.

## Alternatives, for the record

- **A second Chrome from a copy of the profile**, launched by the agents in
  direct debugging mode. No prompts, logins carried over as a snapshot that
  drifts. An hour's setup, no code. Worth trying before the proxy if a copied
  profile is acceptable.
- **Extension-based debugging** (the `chrome.debugger` API), which never touches
  the consent flow. Only for agents built that way.
- **Quiet focus**, already shipped on the branch: a blink instead of a theft.
