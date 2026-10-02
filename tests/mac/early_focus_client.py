"""Opt-in test client: ARM -> ACK -> real CDP connection -> DONE.

Requires websocat on PATH. Results belong outside the repository. This client
does not click consent; run the normal watcher separately for approval.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import select
import shutil
import socket
import subprocess
import time
import uuid


def command(control, reader, token, request_id, op):
    control.sendall((json.dumps({"token": token, "request_id": request_id, "op": op}) + "\n").encode())
    response = reader.readline(4097)
    if not response.endswith(b"\n") or len(response) > 4096:
        raise RuntimeError("control channel closed or invalid reply")
    value = json.loads(response)
    if value.get("request_id") != request_id:
        raise RuntimeError("mismatched request acknowledgement")
    return value["status"]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--session", type=Path)
    source.add_argument("--plain-profile", type=Path, help="baseline connection without ARM")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--timeout", type=float, default=15)
    args = p.parse_args()
    websocket = shutil.which("websocat")
    if not websocket:
        p.error("websocat is required on PATH")
    session = json.loads(args.session.read_text()) if args.session else None
    profile = Path(session["profile"]) if session else args.plain_profile
    endpoint = (profile / "DevToolsActivePort").read_text().splitlines()[:2]
    if session and endpoint != session["endpoint"]:
        p.error("Chrome endpoint changed; restart the experimental guard")
    port, path = endpoint
    if not port.isdigit() or not 0 < int(port) <= 65535 or not path.startswith("/devtools/browser/"):
        p.error("invalid Chrome endpoint")
    url = f"ws://127.0.0.1:{port}{path}"
    request_id = str(uuid.uuid4())
    result = {"request_id": request_id, "granted": False, "connected_after_ack": False}
    proc = None
    with ExitStack() as cleanup:
        control = reader = None
        try:
            if session:
                control = cleanup.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
                control.settimeout(2)
                control.connect(session["socket"])
                reader = cleanup.enter_context(control.makefile("rb"))
            result["arm_status"] = command(control, reader, session["token"], request_id, "ARM") if session else "baseline"
            if session and result["arm_status"] != "armed":
                raise RuntimeError("ARM rejected: " + result["arm_status"])
            result["acked_at"] = time.time() if session else None
            # Do not reuse an ACK across retries: each connection needs its own ARM.
            start = time.monotonic()
            proc = subprocess.Popen([websocket, url], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            result["connected_after_ack"] = session is not None
            proc.stdin.write('{"id":1,"method":"Browser.getVersion"}\n')
            proc.stdin.flush()
            ready, _, _ = select.select([proc.stdout, proc.stderr], [], [], args.timeout)
            result["timed_out"] = not ready
            result["response"] = proc.stdout.readline() if proc.stdout in ready else ""
            result["stderr"] = proc.stderr.readline() if proc.stderr in ready else ""
            result["elapsed_seconds"] = time.monotonic() - start
            if result["response"]:
                reply = json.loads(result["response"])
                result["granted"] = reply.get("id") == 1 and "product" in reply.get("result", {})
            result["done_status"] = command(control, reader, session["token"], request_id, "DONE") if session else None
        except (OSError, ValueError, RuntimeError) as exc:
            result["error"] = str(exc)
        finally:
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["granted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
