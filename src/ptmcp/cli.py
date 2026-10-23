"""ptmcp command line.

ptmcp doctor          check PT, the bridge port and the PT module
ptmcp module          print the PT-side script and install steps
ptmcp exec '<js>'     run JS in PT through a running server
ptmcp serve-bridge    run only the bridge
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

from .bridge import PORT

MODULE = Path(__file__).resolve().parents[2] / "pt_extension" / "ptmcp.js"


def _ping() -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/ping", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def doctor() -> int:
    ok = True
    pt = subprocess.run(["pgrep", "-fl", "PacketTracer"], capture_output=True, text=True).stdout.strip()
    print(("ok" if pt else "--"), "Packet Tracer process", "running" if pt else "not running")
    ok &= bool(pt)
    st = _ping()
    if st is None:
        print("--", f"no ptmcp bridge on 127.0.0.1:{PORT} (starts with the MCP server; or `ptmcp serve-bridge`)")
    else:
        print("ok", f"bridge on :{PORT} mode={st.get('mode')}")
        print(("ok" if st.get("connected") else "--"), "PT module connected",
              st.get("pt") or "(open Extensions > PT-MCP Bridge in Packet Tracer)")
        ok &= bool(st.get("connected"))
    print("  ", f"PT-side module source: {MODULE}")
    return 0 if ok else 1


def exec_js(code: str) -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/exec", data=json.dumps({"code": code, "timeout": 60}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=70) as r:
        env = json.loads(r.read())
    if env.get("bridge_error"):
        print("bridge error:", env["bridge_error"], file=sys.stderr)
        return 1
    print(json.dumps(env.get("value"), indent=1))
    return 0


async def _serve() -> None:
    from .bridge import Bridge
    b = Bridge()
    await b.start()
    print(f"bridge {b.mode} on 127.0.0.1:{b.port}; Ctrl-C to stop", flush=True)
    await asyncio.Event().wait()


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "help"
    if cmd == "doctor":
        sys.exit(doctor())
    if cmd == "module":
        print(f"Install: Packet Tracer > Extensions > Scripting > Configure PT Script Modules > Add... "
              f"{MODULE.with_suffix('.pts')}")
        return
    if cmd == "exec" and len(args) > 1:
        code = args[1]
        if code == "-":
            code = sys.stdin.read()
        sys.exit(exec_js(code))
    if cmd == "serve-bridge":
        try:
            asyncio.run(_serve())
        except KeyboardInterrupt:
            pass
        return
    print(__doc__)


if __name__ == "__main__":
    main()
