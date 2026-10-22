"""Private Chrome + Edge live checks. No installed settings or active profiles.

python tests/windows_multiplex_live.py --output C:/qa/result.json --node node.exe
    --playwright-modules C:/qa/playwright/node_modules
    --puppeteer-modules C:/qa/puppeteer/node_modules
"""
import argparse
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import sys
import traceback

from windows_relay_live import PrivateChrome, Cdp, NoFocus, eventually, http_request
from cdp_multiplex import MultiplexRelay


async def check_browser(args, browser, relay, focus):
    url = f"ws://127.0.0.1:{relay.port}/devtools/browser/yesdev"
    checks = []
    async with AsyncExitStack() as stack:
        clients = [await stack.enter_async_context(Cdp(url)) for _ in range(4)]
        versions = await asyncio.gather(*(c.call("Browser.getVersion") for c in clients))
        assert focus.calls == 1
        checks.append("four concurrent clients, overlapping IDs, one upstream")
        a, b, c, d = clients
        target = (await a.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
        sa = (await a.call("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
        sb = (await b.call("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
        result = await asyncio.gather(a.call("Runtime.evaluate", {"expression": "21*2"}, sa),
                                      b.call("Runtime.evaluate", {"expression": "40+2"}, sb))
        assert all(r["result"]["value"] == 42 for r in result)
        assert "error" in await c.call("Runtime.evaluate", {"expression": "1"}, sa, error=True)
        assert "error" in await c.call("Target.detachFromTarget", {"sessionId": sa}, error=True)
        checks.append("two independent sessions on one page; forged sessions rejected")
        for client in (a, b):
            await client.call("Target.setDiscoverTargets", {"discover": True})
            await client.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        await a.call("Target.setDiscoverTargets", {"discover": False})
        await a.call("Target.setAutoAttach", {"autoAttach": False, "waitForDebuggerOnStart": False, "flatten": True})
        # B must still receive discovery events after A turns its subscription off.
        created = (await c.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
        async with asyncio.timeout(5):
            while True:
                event = json.loads(await b.ws.recv())
                if event.get("method") == "Target.targetCreated" and event["params"]["targetInfo"]["targetId"] == created:
                    break
        assert "sessionId" not in event
        checks.append("independent discovery and auto-attach subscriptions")
        context = (await d.call("Target.createBrowserContext", {"disposeOnDetach": False}))["browserContextId"]
        private_target = (await d.call("Target.createTarget", {"url": "about:blank", "browserContextId": context}))["targetId"]
        assert "error" in await c.call("Target.disposeBrowserContext", {"browserContextId": context}, error=True)
        await d.ws.close()
        await eventually(lambda: len(relay.clients) == 3)
        targets = (await c.call("Target.getTargets"))["targetInfos"]
        assert private_target not in [t["targetId"] for t in targets]
        await a.ws.close()
        await eventually(lambda: len(relay.clients) == 2)
        assert (await b.call("Runtime.evaluate", {"expression": "7*6"}, sb))["result"]["value"] == 42
        checks.append("disconnect disposes owned context and preserves other page session")
        # Library clients overlap with each other and with the raw clients.
        async def library(kind, modules):
            p = await asyncio.create_subprocess_exec(str(args.node), str(Path(__file__).with_name("windows_relay_clients.mjs")),
                    kind, relay.address, str(modules), "--shared", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(p.communicate(), 45)
                assert p.returncode == 0, (kind, out.decode(), err.decode())
                return json.loads(out)
            finally:
                if p.returncode is None:
                    p.kill()
                    await p.wait()
        libraries = await asyncio.gather(library("playwright", args.playwright_modules), library("puppeteer", args.puppeteer_modules))
        assert focus.calls == 1
        checks.append("Playwright and Puppeteer overlap with raw clients on one upstream")
        await c.call("Target.closeTarget", {"targetId": created})
        await c.call("Target.closeTarget", {"targetId": target})
        await c.call("Browser.close")
        await c.ws.wait_closed()
        assert await b.call("Browser.getVersion")
        checks.append("Browser.close disconnects only its caller")
    await eventually(lambda: not relay.clients)
    async with Cdp(url) as reconnected:
        assert await reconnected.call("Browser.getVersion")
    assert focus.calls == 1
    checks.append("idle connection reused by next client")
    assert "403" in (await http_request(relay.port, origin="https://example.com")).splitlines()[0]
    assert "403" in (await http_request(relay.port, host="example.com")).splitlines()[0]
    checks.append("Origin and Host checks reject website requests")
    return dict(product=versions[0]["product"], checks=checks, libraries=libraries, upstream_connections=focus.calls)


async def run(args, report):
    browsers = [PrivateChrome(args.chrome), PrivateChrome(args.edge)]
    relays = []
    try:
        await asyncio.gather(*(browser.start() for browser in browsers))
        focuses = [NoFocus(), NoFocus()]
        for browser, focus in zip(browsers, focuses):
            relays.append(await MultiplexRelay(browser.profile, focus, port=0).start())
        report["browsers"] = await asyncio.gather(*(check_browser(args, browser, relay, focus)
                               for browser, relay, focus in zip(browsers, relays, focuses)))
        # Restart one private browser while a client uses the other browser.
        async with Cdp(f"ws://127.0.0.1:{relays[1].port}/devtools/browser/yesdev") as edge:
            await edge.call("Browser.getVersion")
            await browsers[0].stop()
            await browsers[0].start()
            async with Cdp(f"ws://127.0.0.1:{relays[0].port}/devtools/browser/yesdev") as chrome:
                assert await chrome.call("Browser.getVersion")
            assert await edge.call("Browser.getVersion")
            assert focuses[0].calls == 2 and focuses[1].calls == 1
        report["restart"] = "Chrome restart reconnects Chrome only; Edge remains usable"
        report["passed"] = True
    finally:
        await asyncio.gather(*(relay.close() for relay in relays))
        await asyncio.gather(*(browser.cleanup() for browser in browsers))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--playwright-modules", type=Path, required=True)
    parser.add_argument("--puppeteer-modules", type=Path, required=True)
    parser.add_argument("--chrome", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    parser.add_argument("--edge", default=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    args = parser.parse_args()
    report = dict(passed=False, scope="Private headless Chrome and Edge; no native consent UI")
    try:
        asyncio.run(run(args, report))
    except Exception:
        report.update(passed=False, error=traceback.format_exc())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
