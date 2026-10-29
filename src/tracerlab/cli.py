"""tracerlab command line.

tracerlab doctor             check PT, the bridge port and the PT module
tracerlab install-extension  copy the PT script module into Packet Tracer
tracerlab module             print the PT-side script and install steps
tracerlab exec '<js>'        run JS in PT through a running server
tracerlab serve-bridge       run only the bridge
"""

from __future__ import annotations

import asyncio
import glob
import json
import platform
import shutil
import subprocess
import sys
import urllib.request
from importlib import resources
from pathlib import Path

from .bridge import PORT

MODULE_NAME = "ptmcp.pts"


def module_path() -> Path:
    return Path(str(resources.files("tracerlab").joinpath("pt_extension", MODULE_NAME)))


def pt_extension_dirs() -> list[Path]:
    s = platform.system()
    if s == "Darwin":
        pats = ["/Applications/Cisco Packet Tracer*/Cisco Packet Tracer*.app/Contents/extensions"]
    elif s == "Windows":
        pats = [r"C:\Program Files\Cisco Packet Tracer*\extensions",
                r"C:\Program Files (x86)\Cisco Packet Tracer*\extensions"]
    else:
        pats = ["/opt/pt/extensions", "/usr/local/pt/extensions",
                str(Path.home() / "pt" / "extensions")]
    return [Path(p) for pat in pats for p in sorted(glob.glob(pat))]


def install_extension(dest: str | None = None) -> int:
    src = module_path()
    if not src.is_file():
        print("could not find the bundled PT module", file=sys.stderr)
        return 1

    targets = [Path(dest)] if dest else pt_extension_dirs()
    if not targets:
        print("no Packet Tracer install found; pass the extensions directory:", file=sys.stderr)
        print("  tracerlab install-extension '/path/to/Packet Tracer/extensions'", file=sys.stderr)
        return 1

    for ext in targets:
        if not ext.is_dir():
            print("--", f"{ext} is not a directory", file=sys.stderr)
            return 1
        out = ext / "ptmcp"
        try:
            out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out / MODULE_NAME)
        except PermissionError:
            print("--", f"cannot write to {out}; rerun with sudo", file=sys.stderr)
            return 1
        print("ok", f"installed {out / MODULE_NAME}")

    print("   restart Packet Tracer; when it asks about a new script module, choose Yes")
    return 0


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

    installed = [e / "ptmcp" / MODULE_NAME for e in pt_extension_dirs()]
    found = [p for p in installed if p.is_file()]
    print(("ok" if found else "--"), "PT script module",
          str(found[0]) if found else "not installed (run `tracerlab install-extension`)")

    st = _ping()
    if st is None:
        print("--", f"no ptmcp bridge on 127.0.0.1:{PORT} (starts with the MCP server; or `tracerlab serve-bridge`)")
    else:
        print("ok", f"bridge on :{PORT} mode={st.get('mode')}")
        print(("ok" if st.get("connected") else "--"), "PT module connected",
              st.get("pt") or "(open Extensions > PT-MCP Bridge in Packet Tracer)")
        ok &= bool(st.get("connected"))
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
    if cmd in ("install-extension", "install"):
        sys.exit(install_extension(args[1] if len(args) > 1 else None))
    if cmd == "module":
        print(f"Install: Packet Tracer > Extensions > Scripting > Configure PT Script Modules > Add... "
              f"{module_path()}")
        print("Or just run: tracerlab install-extension")
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
