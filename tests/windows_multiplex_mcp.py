"""Four fresh stdio MCP servers against two private browser relays.

Supply an installed chrome-devtools-mcp bin path with --mcp-bin. This never
uses or changes an existing MCP server, daemon, configuration, or browser.
"""
import argparse
import asyncio
import json
from pathlib import Path
import re
import tempfile
import traceback

from windows_relay_live import PrivateChrome, NoFocus
from cdp_multiplex import MultiplexRelay


async def client(args, relay, number):
    with tempfile.TemporaryFile() as errors:
        process = await asyncio.create_subprocess_exec(str(args.node), str(args.mcp_bin),
                    "--browserUrl", relay.address, "--no-usage-statistics", "--no-performance-crux",
                    "--no-category-performance", stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=errors)
        counter = 0
        async def send(method, params, notification=False):
            nonlocal counter
            counter += 1
            request = dict(jsonrpc="2.0", method=method, params=params)
            if not notification:
                request["id"] = counter
            process.stdin.write((json.dumps(request) + "\n").encode())
            await process.stdin.drain()
            if notification:
                return
            async with asyncio.timeout(40):
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        errors.seek(0)
                        raise AssertionError(errors.read().decode(errors="replace"))
                    reply = json.loads(line)
                    if reply.get("id") == counter:
                        assert "error" not in reply, reply
                        result = reply.get("result", {})
                        assert not result.get("isError"), result
                        return result
        try:
            initialized = await send("initialize", dict(protocolVersion="2025-06-18", capabilities={},
                                      clientInfo=dict(name="yesdev-isolated-test", version="1")))
            await send("notifications/initialized", {}, notification=True)
            await send("tools/call", dict(name="list_pages", arguments={}))
            marker = f"relay-mcp-{number}"
            page = await send("tools/call", dict(name="new_page", arguments={"url": f"data:text/html,<title>{marker}</title>"}))
            text = "\n".join(c.get("text", "") for c in page.get("content", []))
            selected = re.search(r"(?:^|\n)(\d+):[^\n]*" + marker, text)
            assert selected, text
            result = await send("tools/call", dict(name="evaluate_script", arguments={"function": "() => 6 * 7", "pageId": int(selected[1])}))
            assert "42" in json.dumps(result), result
            await send("tools/call", dict(name="close_page", arguments={"pageId": int(selected[1])}))
            return dict(server=initialized["serverInfo"], checks=["list_pages", "new_page", "evaluate_script=42", "close_page"])
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()


async def run(args, report):
    browsers = [PrivateChrome(args.chrome), PrivateChrome(args.edge)]
    relays = []
    try:
        await asyncio.gather(*(browser.start() for browser in browsers))
        focuses = [NoFocus(), NoFocus()]
        for browser, focus in zip(browsers, focuses):
            relays.append(await MultiplexRelay(browser.profile, focus, port=0).start())
        report["clients"] = await asyncio.gather(*(client(args, relay, number)
                              for relay in relays for number in range(2)))
        assert [focus.calls for focus in focuses] == [1, 1]
        report.update(passed=True, upstream_connections=[focus.calls for focus in focuses])
    finally:
        await asyncio.gather(*(relay.close() for relay in relays))
        await asyncio.gather(*(browser.cleanup() for browser in browsers))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--mcp-bin", type=Path, required=True)
    parser.add_argument("--chrome", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    parser.add_argument("--edge", default=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    args = parser.parse_args()
    report = dict(passed=False, scope="Two independent MCP clients per private browser, Chrome and Edge together")
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
