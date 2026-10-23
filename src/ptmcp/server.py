"""MCP server. pt_* tools talk to a running Packet Tracer, the rest work offline."""

from __future__ import annotations

import asyncio
import base64
import gzip
import ipaddress
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations

from . import __version__, catalog, configgen, labs, netcalc, pkt, planner
from .bridge import Bridge, NotConnected, PTError

log = logging.getLogger("ptmcp")

INSTRUCTIONS = f"""Cisco Packet Tracer 9 control (ptmcp {__version__}).

Workflow that works best:
1. pt_status: confirms PT is connected (if not, run pt_setup_instructions and tell the user).
2. Read before writing: pt_topology (whole canvas, compact) / pt_device / pt_running_config.
3. Build: pt_lab for a complete pre-computed lab, or pt_build for a custom spec in ONE call
   (devices + modules + cabling with automatic port/cable choice + configs + host IPs + auto addressing).
   Smaller edits: pt_add_devices, pt_connect (ports optional - free ports are picked), pt_cli, pt_configure_hosts.
4. Addressing/routing: pt_auto_address assigns every subnet, interface IP, host IP/gateway and generates
   OSPF/EIGRP/RIP/static routing from the live cabling. Use it instead of hand-computing IP plans.
5. Feature configs: ios_generate / pt_apply_feature produce correct IOS for VLANs, trunks, OSPF, DHCP,
   NAT, ACLs, HSRP, EtherChannel, SSH, port-security... Prefer them over writing IOS by hand.
6. Verify: pt_ping / pt_ping_matrix / pt_traceroute, then pt_diagnose for misconfigurations
   (wrong gateways, mixed subnets, down interfaces, missing routes with deep=true).
Device names are case-sensitive. Interface names must be full PT names (GigabitEthernet0/0).
pt_run_js + pt_api_lookup are the escape hatch for anything the tools do not cover.
"""

bridge = Bridge()


@asynccontextmanager
async def lifespan(_server):
    try:
        await bridge.start()
    except PTError as e:
        log.error("%s", e)
    try:
        yield {}
    finally:
        await bridge.stop()


mcp = MCPServer("packet-tracer", instructions=INSTRUCTIONS, version=__version__, lifespan=lifespan)


def _j(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"))


async def call(fn: str, *args: Any, timeout: float = 60.0) -> Any:
    """Invoke PTM.<fn>(*args) inside Packet Tracer."""
    return await bridge.run(f"return PTM.{fn}({','.join(_j(a) for a in args)});", timeout)


async def topo(detail: bool = False, gw: bool = True, configs: bool = False) -> planner.Topology:
    """Live topology. configs=True also reads every IOS config so segments are VLAN-aware."""
    if configs:
        snap, cfgs = await asyncio.gather(call("snapshot", {"detail": detail, "gw": gw}), call("configs"))
        return planner.Topology(snap, cfgs)
    snap = await call("snapshot", {"detail": detail, "gw": gw})
    return planner.Topology(snap)


def _err(e: Exception) -> str:
    if isinstance(e, NotConnected):
        return f"NOT CONNECTED: {e}"
    return f"ERROR: {e}"


RO = ToolAnnotations(read_only_hint=True, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(destructive_hint=True, open_world_hint=False)


def tool(fn=None, *, ro: bool = False, destructive: bool = False):
    """Register a live tool; PT/bridge exceptions become one-line error strings for the model."""
    import functools

    def deco(f):
        @functools.wraps(f)
        async def wrapper(*a, **kw):
            try:
                return await f(*a, **kw)
            except (PTError, ValueError, KeyError, TypeError) as e:
                return _err(e)
        ann = RO if ro else DESTRUCTIVE if destructive else ToolAnnotations(open_world_hint=False)
        return mcp.tool(annotations=ann, structured_output=False)(wrapper)
    return deco(fn) if fn else deco


def _pairs_text(rows: list[tuple[str, str]]) -> str:
    return "\n".join(f"{a}: {b}" for a, b in rows)


IOS_ERR = re.compile(r"^\s*%\s*(Invalid|Incomplete|Ambiguous|Unknown|Unrecognized|Bad|Error|.*not.*(?:allowed|supported))",
                     re.I | re.M)
STATUS = {0: "ok", 1: "ambiguous command", 2: "invalid input", 3: "incomplete command", 4: "not implemented in PT"}
EXPECTED_ERR = re.compile(r"clock rate|DCE|already|Nothing to|NVRAM", re.I)


def _cli_report(device: str, res: dict, show_output: bool) -> tuple[str, int]:
    errors, outs = [], []
    for cmd, status, out in res["r"]:
        out = (out or "").replace("\r", "")
        bad = status not in (0, None) or bool(IOS_ERR.search(out))
        if bad:
            msg = STATUS.get(status, f"status {status}")
            m = IOS_ERR.search(out)
            if m:
                msg = out[m.start():].strip().splitlines()[0]
            expected = bool(EXPECTED_ERR.search(cmd) or EXPECTED_ERR.search(msg))
            errors.append(f"  {'(expected) ' if expected else ''}{cmd.strip()}  -> {msg}")
        if show_output:
            outs.append(out)
    n_bad = sum(1 for e in errors if "(expected)" not in e)
    if show_output:
        return "\n".join(outs).strip(), n_bad
    head = f"{device}: {len(res['r'])} lines, {n_bad} error{'s' if n_bad != 1 else ''}"
    return head + ("\n" + "\n".join(errors) if errors else ""), n_bad


async def run_config(device: str, lines: list[str], save: bool = False) -> tuple[str, int]:
    lines = [l for l in lines if l.strip() and not l.strip().startswith("!")]
    if not lines:
        return f"{device}: nothing to apply", 0
    res = await call("cli", device, lines, "config", timeout=30 + len(lines) * 0.5)
    report = _cli_report(device, res, False)
    if save:
        await call("cli", device, ["write memory"], "exec")
    return report


def _as_list(v: str | list[str]) -> list[str]:
    if isinstance(v, str):
        return [l for l in v.replace("\r", "").split("\n") if l.strip()]
    return list(v)


def _auto_layout(devs: list[dict], existing: int = 0) -> None:
    """Fill missing x/y: routers top row, switches middle, hosts bottom."""
    rows = {"router": 100, "firewall": 100, "l3-switch": 220, "switch": 260, "host": 420}
    counters: dict[int, int] = {}
    for d in devs:
        if d.get("x") is not None and d.get("y") is not None:
            continue
        try:
            role = catalog.resolve_model(d["model"]).role
        except KeyError:
            role = "host"
        y = rows.get(role, 340)
        i = counters.get(y, 0)
        counters[y] = i + 1
        d["x"], d["y"] = 120 + i * 140 + (existing % 3) * 20, y


async def _resolve_ip(target: str, t: planner.Topology | None) -> str:
    try:
        ipaddress.ip_address(target)
        return target
    except ValueError:
        pass
    t = t or await topo()
    dev = t.devices.get(target)
    if not dev:
        raise ValueError(f"'{target}' is neither an IP nor a device name")
    for p in dev.ports.values():
        if p.iface and p.peer_dev:
            return str(p.iface.ip)
    for p in dev.ports.values():
        if p.iface:
            return str(p.iface.ip)
    raise ValueError(f"{target} has no IP address yet")


@tool(ro=True)
async def pt_status() -> str:
    """Bridge + Packet Tracer status: connection, PT version, open file, device/link counts."""
    st = bridge.status()
    if not st.get("connected") and st.get("mode") != "relay":
        return (f"Bridge listening on 127.0.0.1:{bridge.port} but Packet Tracer is NOT connected.\n"
                "Open Packet Tracer 9; the 'PT-MCP Bridge' window must show 'connected'. "
                "First time? Run pt_setup_instructions.")
    info = await call("info")
    return (f"connected ({st.get('mode')}) | PT {info.get('version')} | devices {info.get('devices')} | "
            f"links {info.get('links')} | mode {'simulation' if info.get('sim') else 'realtime'}"
            + (f" | file {info['file']}" if info.get("file") else ""))


@mcp.tool(annotations=RO, structured_output=False)
async def pt_setup_instructions() -> str:
    """How to install the one-time PT-side bridge module (needed if pt_status says NOT CONNECTED)."""
    ext = Path(__file__).resolve().parents[2] / "pt_extension"
    return f"""One-time Packet Tracer setup (30 seconds):
1. Packet Tracer 9 > Extensions > Scripting > Configure PT Script Modules... > Add...
2. Choose {ext / 'ptmcp.pts'} (Startup is already "On Startup").
3. Restart Packet Tracer (or select the module and press Start). A small "PT-MCP Bridge" window opens
   and turns green ("connected") while this MCP server runs. Reopen it from Extensions > PT-MCP Bridge.
The window only loads a stub; the bridge logic is served by this server, so updates need no reinstall.
Rebuilding the module from source: see {ext / 'ptmcp.js'} header.
Bridge port: {bridge.port} (env PTMCP_PORT; must match PTMCP_PORT in the module's bridge.html)."""


@tool(ro=True)
async def pt_topology(detail: bool = False) -> str:
    """Whole canvas, compact: every device (model, position, power) with its addressed/cabled interfaces,
    peers and link state. detail=true adds MACs, switchport modes, descriptions and unused ports."""
    t = await topo(detail=detail)
    return t.text(detail=detail)


@tool(ro=True)
async def pt_device(name: str) -> str:
    """One device in depth: all physical ports (IP, peer, state, switchport mode), free ports, module slots."""
    snap = await call("snapshot", {"detail": True, "all": True, "gw": True})
    t = planner.Topology({"devices": [d for d in snap["devices"] if d["n"] == name], "links": []})
    if name not in t.devices:
        names = ", ".join(sorted(d["n"] for d in snap["devices"]))
        return f"ERROR: no device '{name}'. Devices: {names}"
    out = [t.text(detail=True)]
    try:
        mods = await call("modules", name)
        slots = [f"{p}:{m or '(empty)'}" for p, _, m in mods.get("slots", [])]
        if slots:
            out.append("slots: " + ", ".join(slots))
    except PTError:
        pass
    return "\n".join(out)


@tool(ro=True)
async def pt_running_config(device: str, startup: bool = False, section: str | None = None) -> str:
    """Running (or startup) config of an IOS device, read from the device model (fast, no paging).
    section: only blocks whose header contains this text, e.g. 'interface', 'router ospf', 'line vty'."""
    cfg = await call("runningConfig", device, startup)
    if not cfg:
        res = await call("cli", device, ["show startup-config" if startup else "show running-config"], "exec", timeout=60)
        cfg = "\n".join(o for _, _, o in res["r"])
    lines = [l for l in cfg.replace("\r", "").split("\n") if l.strip() != "!"]
    if section:
        sec_rx = re.compile(re.escape(section) + (r"(?![\d/.:])" if section[-1:].isdigit() else ""), re.I)
        keep, on = [], False
        for l in lines:
            if not l.startswith(" "):
                on = bool(sec_rx.search(l))
            if on:
                keep.append(l)
        lines = keep or [f"(no section matching '{section}')"]
    return "\n".join(lines).strip()


@tool(ro=True)
async def pt_show(device: str, commands: list[str] | str, grep: str | None = None) -> str:
    """Run show/exec commands (privileged EXEC) on a router/switch and return their output.
    commands: one or more, e.g. ['show ip interface brief', 'show ip route'].
    grep: optional regex; keep only matching lines (like '| include')."""
    cmds = _as_list(commands)
    res = await call("cli", device, cmds, "exec", timeout=30 + 5 * len(cmds))
    chunks = []
    for cmd, status, out in res["r"]:
        out = (out or "").replace("\r", "").strip("\n")
        if grep:
            rx = re.compile(grep, re.I)
            out = "\n".join(l for l in out.split("\n") if rx.search(l))
        if not out and status:
            out = f"% {STATUS.get(status, status)}"
        chunks.append(f"{device}# {cmd}\n{out}" if len(cmds) > 1 else out)
    text = "\n\n".join(chunks).strip()
    if not text and not res.get("hooked"):
        return "No output captured (PT did not expose terminal output to scripts). Try pt_running_config."
    return text or "(no output)"


@tool(ro=True)
async def pt_command_log(device: str | None = None, limit: int = 40) -> str:
    """Recent commands typed into any device CLI (PT's command log), oldest first."""
    rows = await call("cmdLog", limit, device)
    if not rows:
        return "(command log is empty)"
    return "\n".join(f"{t} {d} {p}{c}" for t, d, p, c in rows)


@tool(ro=True)
async def pt_screenshot(fmt: Literal["PNG", "JPG"] = "PNG") -> Image:
    """Image of the logical workspace (the topology as drawn in PT)."""
    data = await call("image", fmt, timeout=60)
    return Image(data=base64.b64decode(data), format=fmt.lower().replace("jpg", "jpeg"))


@tool
async def pt_add_devices(devices: list[dict]) -> str:
    """Add devices. devices: [{name, model, x?, y?}], model like '2911', 'ISR4331', '2960-24TT', '3560-24PS',
    'PC-PT', 'Laptop-PT', 'Server-PT' (see pt_catalog). Missing x/y are laid out automatically.
    Routers/switches have their boot skipped so they are ready immediately."""
    devs = [dict(d) for d in devices]
    _auto_layout(devs)
    items = []
    for d in devs:
        model, types = catalog.model_or_passthrough(d["model"])
        items.append({"name": d["name"], "model": model, "types": types, "x": d["x"], "y": d["y"]})
    res = await call("addDevices", items, timeout=30 + 2 * len(items))
    ok = [r["name"] for r in res if r.get("ok")]
    bad = [f"{r['name']}: {r.get('error')}" for r in res if not r.get("ok")]
    return f"added {len(ok)}: {', '.join(ok)}" + ("\nfailed:\n  " + "\n  ".join(bad) if bad else "")


@tool
async def pt_connect(links: list[dict]) -> str:
    """Cable devices. links: [{a, b, a_port?, b_port?, cable?}].
    Ports are optional: the first free port of the right media is chosen (fastest first; on switches use
    a_pref/b_pref='access' to keep gigabit uplinks free). cable: auto (default) | straight | cross | serial |
    fiber | console | coaxial | phone. For serial links the device needs a serial module (pt_add_module)."""
    items = [{"a": l["a"], "b": l["b"], "ap": l.get("a_port"), "bp": l.get("b_port"), "cable": l.get("cable", "auto"),
              "prefA": l.get("a_pref", "fast"), "prefB": l.get("b_pref", "fast")} for l in links]
    res = await call("connect", items, timeout=30 + len(items))
    lines = []
    for r in res:
        if r.get("ok"):
            lines.append(f"ok  {r['a']}:{r['ap']} <-> {r['b']}:{r['bp']} ({r['cable']})")
        else:
            lines.append(f"ERR {r['a']} <-> {r['b']}: {r.get('error')}")
    return "\n".join(lines)


@tool(destructive=True)
async def pt_disconnect(ports: list[str]) -> str:
    """Remove cables by endpoint: ['R1:GigabitEthernet0/0', 'S1:FastEthernet0/3']."""
    items = [{"device": p.split(":", 1)[0], "port": p.split(":", 1)[1]} for p in ports]
    res = await call("unlink", items)
    return "\n".join(f"{p}: {'removed' if r == 1 else ('no cable' if r == 0 else r)}" for p, r in zip(ports, res))


@tool(destructive=True)
async def pt_delete_devices(names: list[str]) -> str:
    """Delete devices (and their cables)."""
    res = await call("remove", names)
    return ", ".join(f"{k}: {'deleted' if v == 1 else v}" for k, v in res.items())


@tool(destructive=True)
async def pt_clear_canvas(confirm: bool = False) -> str:
    """Delete EVERY device on the canvas. Requires confirm=true."""
    if not confirm:
        return "Refusing: pass confirm=true to delete every device."
    res = await call("clearAll", timeout=120)
    return f"deleted {sum(1 for v in res.values() if v == 1)} devices"


@tool
async def pt_update_device(name: str, new_name: str | None = None, x: int | None = None, y: int | None = None,
                           power: bool | None = None) -> str:
    """Rename, move and/or power a device on/off."""
    done = []
    if x is not None and y is not None:
        await call("move", [{"name": name, "x": x, "y": y}])
        done.append(f"moved to ({x},{y})")
    if power is not None:
        await call("power", [name], power)
        done.append("powered " + ("on" if power else "off"))
    if new_name:
        await call("rename", name, new_name)
        done.append(f"renamed to {new_name}")
    return f"{name}: " + (", ".join(done) or "nothing to do")


@tool
async def pt_add_module(device: str, module: str, slot: str | None = None) -> str:
    """Insert a module (device is power-cycled automatically). Common: HWIC-2T (2 serial, 1941/2901/2911),
    WIC-2T (1841/2811), NIM-2T (ISR4321/4331), HWIC-4ESW, NM-1FE-TX, PT-ROUTER-NM-1S (Router-PT),
    AC-POWER-SUPPLY (3650). slot omitted = first slot that accepts it. Returns the device's ports afterwards."""
    mtype = catalog.MODULE_TYPES.get(module)
    if mtype is None:
        cand = [m for m in catalog.MODULE_TYPES if m.lower() == module.lower()]
        if not cand:
            return f"ERROR: unknown module {module}. Known: {', '.join(sorted(catalog.MODULE_TYPES))}"
        module, mtype = cand[0], catalog.MODULE_TYPES[cand[0]]
    slots = [slot] if slot else labs.SERIAL_SLOTS + [str(i) for i in range(3, 10)]
    last = None
    for s in slots:
        try:
            ports = await call("addModule", device, s, module, mtype)
            names = [p[0] for p in ports]
            return f"{device}: {module} inserted in slot {s}. Ports now: {', '.join(names)}"
        except PTError as e:
            last = e
    return _err(last or PTError("no slot accepted the module"))


@tool(ro=True)
async def pt_device_modules(device: str) -> str:
    """Module slot tree of a device: slot path, slot type and installed module."""
    m = await call("modules", device)
    rows = [f"{p}  type={t}  {mod or '(empty)'}" for p, t, mod in m.get("slots", [])]
    # entries look like "NAME:<image path><description>"
    sup = sorted({str(x).split(":", 1)[0].strip() for x in (m.get("supported") or []) if str(x).strip()})
    return f"{device} [{m.get('model')}]\n" + "\n".join(rows) + (f"\nsupported: {', '.join(sup)}" if sup else "")


async def _build(spec: dict, verify: bool = False) -> str:
    """Shared pipeline for pt_build and pt_lab."""
    report: list[str] = []
    devs = [dict(d) for d in spec.get("devices", [])]
    _auto_layout(devs)
    if devs:
        items = []
        for d in devs:
            model, types = catalog.model_or_passthrough(d["model"])
            items.append({"name": d["name"], "model": model, "types": types, "x": d["x"], "y": d["y"]})
        res = await call("addDevices", items, timeout=60 + 2 * len(items))
        bad = [f"{r['name']}: {r.get('error')}" for r in res if not r.get("ok")]
        report.append(f"devices: {len(res) - len(bad)} added" + (f"; FAILED {'; '.join(bad)}" if bad else ""))
    added: dict[str, int] = {}
    for d in devs:
        for m in d.get("modules") or []:
            mtype = catalog.MODULE_TYPES.get(m["module"])
            slots = m.get("slots") or ([m["slot"]] if m.get("slot") else labs.SERIAL_SLOTS)
            ok = False
            for s in slots:
                try:
                    await call("addModule", d["name"], s, m["module"], mtype)
                    ok = True
                    break
                except PTError:
                    continue
            if ok:
                added[m["module"]] = added.get(m["module"], 0) + 1
            else:
                report.append(f"module {m['module']} -> {d['name']}: FAILED (no free compatible slot)")
    if added:
        report.append("modules: " + ", ".join(f"{n}x {mod}" for mod, n in added.items()))
    chosen = []
    if spec.get("links"):
        items = [{"a": l["a"], "b": l["b"], "ap": l.get("ap") or l.get("a_port"), "bp": l.get("bp") or l.get("b_port"),
                  "cable": l.get("cable") or "auto", "prefA": l.get("prefA", "fast"), "prefB": l.get("prefB", "fast")}
                 for l in spec["links"]]
        res = await call("connect", items, timeout=60 + len(items))
        okc = [r for r in res if r.get("ok")]
        chosen = [(r["a"], r["ap"], r["b"], r["bp"], r["cable"]) for r in okc]
        report.append(f"links: {len(okc)}/{len(res)} cabled")
        report += [f"  FAILED {r['a']}<->{r['b']}: {r.get('error')}" for r in res if not r.get("ok")]
    cmds: dict[str, list[str]] = {k: list(v) for k, v in (spec.get("configs") or {}).items()}
    hosts: dict[str, dict] = dict(spec.get("hosts") or {})
    if spec.get("auto") is not None:
        t = await topo(configs=True)
        plan = planner.auto_address(t, **spec["auto"])
        for dev, lines in plan.device_cmds.items():
            cmds.setdefault(dev, []).extend(lines)
        for h, cfg in plan.host_cfg.items():
            hosts.setdefault(h, cfg)
        report.append(plan.text())
    post = spec.get("post")
    if post:
        for dev, lines in post({"links": chosen}).items():
            cmds.setdefault(dev, []).extend(lines)
    errors = 0
    if cmds:
        results = await asyncio.gather(*(run_config(dev, lines, save=True) for dev, lines in cmds.items()))
        for text, n_bad in results:
            errors += n_bad
            report.append(text if n_bad or "(expected)" in text else text.split("\n")[0])
    if hosts:
        report.append(await _configure_hosts([{"name": k, **v} for k, v in hosts.items()]))
    report.append(f"config errors: {errors}")
    checks = spec.get("checks") or []
    if verify and checks:
        report.append(await _converge())
        report.append("verification:\n" + await _ping_pairs([list(c) for c in checks], count=2))
    elif checks:
        report.append("verify with pt_ping pairs=" + _j([list(c) for c in checks[:6]]))
    for n in spec.get("notes") or []:
        report.append(f"note: {n}")
    return "\n".join(report)


@tool
async def pt_build(spec: dict, verify: bool = False) -> str:
    """Build a whole network in one call. spec = {
      devices: [{name, model, x?, y?, modules?: [{module, slot?}]}],
      links:   [{a, b, a_port?, b_port?, cable?}],           # ports auto-picked when omitted
      configs: {device: ["ios line", ...]},                  # global-config lines, applied + saved
      hosts:   {pc: {ip, mask | cidr, gateway, dns} | {dhcp: true}},
      auto:    {routing: 'ospf'|'eigrp'|'rip'|'static'|'none', lan_pool, p2p_pool, dhcp, lan_prefix},  # optional
      checks:  [[src, dst], ...]                             # ping pairs, run when verify=true
    }
    `auto` runs pt_auto_address after cabling, so addresses fit whatever ports were chosen."""
    return await _build(spec, verify=verify)


@tool
async def pt_lab(name: str = "list", params: dict | None = None, verify: bool = False, clear_first: bool = False) -> str:
    """Build a complete, pre-computed lab (devices, cabling, IPs, configs, hosts, test pairs) in one call.
    name='list' shows blueprints and their params: small-office, router-on-a-stick, intervlan-l3,
    routing (ospf|eigrp|rip|static x chain|ring|star|mesh, optional serial), nat-internet, hsrp,
    etherchannel-stp, ipv6-dual-stack. verify=true pings the test pairs after convergence."""
    if name == "list":
        return labs.lab_list()
    if name not in labs.LABS:
        return f"ERROR: unknown lab {name}. Try name='list'."
    L = labs.LABS[name](**(params or {}))
    if clear_first:
        await call("clearAll", timeout=120)
    else:
        t = await topo(gw=False)
        clash = [d["name"] for d in L.devices if d["name"] in t.devices]
        if clash:
            return f"ERROR: devices already exist: {', '.join(clash)}. Use clear_first=true or delete them."
    spec = {"devices": L.devices, "links": L.links, "configs": L.cmds, "hosts": L.hosts, "auto": L.auto,
            "post": L.post, "checks": L.checks, "notes": L.notes}
    body = await _build(spec, verify=verify)
    return f"## {L.title}\n{body}"


@tool
async def pt_cli(device: str, commands: list[str] | str, mode: Literal["config", "exec"] = "config",
                 save: bool = False) -> str:
    """Run IOS commands on a router/switch/ASA.
    mode=config: lines are entered from global config (sub-modes like 'interface g0/0' work, 'end' is added);
    the reply lists only failing lines. mode=exec: privileged EXEC, returns output (prefer pt_show).
    save=true appends 'write memory'."""
    lines = _as_list(commands)
    if mode == "exec":
        res = await call("cli", device, lines, "exec", timeout=30 + 5 * len(lines))
        return _cli_report(device, res, True)[0] or "(no output)"
    text, _ = await run_config(device, lines, save)
    return text


async def _configure_hosts(hosts: list[dict]) -> str:
    out = []
    for h in hosts:
        c = {k: h.get(k) for k in ("ip", "mask", "dns", "port", "dhcp", "ipv6", "gw6")}
        c["gw"] = h.get("gateway") or h.get("gw")
        if h.get("cidr") or (c.get("ip") and "/" in str(c["ip"])):
            i = ipaddress.ip_interface(h.get("cidr") or c["ip"])
            c["ip"], c["mask"] = str(i.ip), str(i.network.netmask)
        try:
            port = await call("hostConfig", h["name"], c)
            desc = "dhcp" if c.get("dhcp") else f"{c.get('ip')}/{netcalc.IPv4Net('0.0.0.0/' + c['mask']).prefixlen}" \
                if c.get("ip") and c.get("mask") else "updated"
            out.append(f"{h['name']}:{port} {desc}" + (f" gw {c['gw']}" if c.get("gw") and not c.get("dhcp") else ""))
        except PTError as e:
            out.append(f"{h['name']}: ERROR {e}")
    return "hosts: " + "; ".join(out)


@tool
async def pt_configure_hosts(hosts: list[dict]) -> str:
    """IP settings for PCs/laptops/servers/printers. hosts: [{name, ip, mask? | ip:'10.0.0.5/24' | cidr,
    gateway?, dns?, dhcp?: true, port?, ipv6?: '2001:db8::5/64', gw6?}]."""
    return await _configure_hosts(hosts)


@tool
async def pt_apply_feature(device: str, feature: str, params: dict, dry_run: bool = False) -> str:
    """Generate IOS config for a feature (see ios_features) and apply it to a device in one step.
    e.g. feature='ospf', params={'networks':['10.0.0.0/30','192.168.1.0/24'],'router_id':'1.1.1.1',
    'passive':['GigabitEthernet0/0']}. dry_run=true returns the lines without applying."""
    lines = configgen.generate(feature, params)
    if dry_run:
        return "\n".join(lines)
    text, _ = await run_config(device, lines)
    return text + "\n" + f"({len(lines)} generated lines; dry_run=true to view them)"


@tool
async def pt_auto_address(routing: Literal["ospf", "eigrp", "rip", "static", "none"] = "ospf",
                          lan_pool: str = "192.168.0.0/16", p2p_pool: str = "10.0.0.0/24",
                          lan_prefix: int | None = 24, dhcp: bool = False, dns: str | None = None,
                          overwrite: bool = False, loopbacks: bool = False, apply: bool = True) -> str:
    """Address the live topology and generate routing, computed from the actual cabling:
    each L3 segment (router/host ports joined through switches) gets a subnet: LANs from lan_pool
    (/lan_prefix, or right-sized when null), router-only links get /30s from p2p_pool. Routers take the
    first host addresses (gateway), hosts .10+ (or DHCP pools on the router with dhcp=true).
    Existing addresses are kept unless overwrite=true. apply=false only shows the plan and configs."""
    t = await topo(configs=True)
    plan = planner.auto_address(t, lan_pool=lan_pool, p2p_pool=p2p_pool, lan_prefix=lan_prefix, routing=routing,
                                dhcp=dhcp, dns=dns, overwrite=overwrite, loopbacks=loopbacks)
    out = [plan.text()]
    if not apply:
        for dev, lines in plan.device_cmds.items():
            out.append(f"\n[{dev}]\n" + "\n".join(lines))
        for h, c in plan.host_cfg.items():
            out.append(f"[{h}] {c}")
        return "\n".join(out)
    results = await asyncio.gather(*(run_config(d, l, save=True) for d, l in plan.device_cmds.items()))
    out += [r[0] for r in results]
    if plan.host_cfg:
        out.append(await _configure_hosts([{"name": k, **v} for k, v in plan.host_cfg.items()]))
    out.append("routing converges in ~10-40s of PT time; pt_fast_forward speeds it up.")
    return "\n".join(out)


@tool
async def pt_save_configs(devices: list[str] | None = None) -> str:
    """'write memory' on the given (default: all) routers/switches."""
    t = await topo(gw=False)
    names = devices or [d.name for d in t.devices.values() if d.has_cli]
    res = await asyncio.gather(*(call("cli", n, ["write memory"], "exec") for n in names), return_exceptions=True)
    return ", ".join(f"{n}: {'saved' if not isinstance(r, Exception) else r}" for n, r in zip(names, res))


PING_DONE = re.compile(r"(Lost = \d+|Success rate is \d+ percent|Ping request could not find host|% Unrecognized host)")
TRACE_DONE = re.compile(r"(Trace complete|^\s*30\s|\n\s*\d+\s+\S*\s*\*\s+\*\s+\*\s*\n.*\n.*\*\s+\*\s+\*)", re.M)


def _ping_summary(out: str) -> str:
    m = re.search(r"Sent = (\d+), Received = (\d+), Lost = (\d+)", out)
    if m:
        sent, rec = int(m.group(1)), int(m.group(2))
        rtt = re.search(r"Average = (\d+)ms", out)
        verdict = "OK" if rec == sent else ("PARTIAL" if rec else "FAIL")
        replies = re.findall(r"(Reply from|Request timed out|Destination host unreachable)", out)
        lost_first = next((i for i, r in enumerate(replies) if r == "Reply from"), len(replies))
        if verdict == "PARTIAL" and 0 < lost_first <= 3 and all(r == "Reply from" for r in replies[lost_first:]):
            # leading losses are ARP on a fresh path
            which = "1st" if lost_first == 1 else f"first {lost_first}"
            return f"OK {rec}/{sent} ({which} lost to ARP)" + (f" avg {rtt.group(1)}ms" if rtt else "")
        why = ""
        if not rec:
            u = re.search(r"(Destination host unreachable|Request timed out)", out)
            why = f" ({u.group(1)})" if u else ""
        return f"{verdict} {rec}/{sent}" + (f" avg {rtt.group(1)}ms" if rtt else "") + why
    m = re.search(r"Success rate is (\d+) percent \((\d+)/(\d+)\)", out)
    if m:
        pct = int(m.group(1))
        return f"{'OK' if pct == 100 else 'PARTIAL' if pct else 'FAIL'} {m.group(2)}/{m.group(3)}"
    if "could not find host" in out or "Unrecognized host" in out:
        return "FAIL (unknown host)"
    return "TIMEOUT"


async def _wait_dhcp(names: list[str], t: planner.Topology, wait: float = 20.0) -> planner.Topology:
    need = [n for n in names if n in t.devices and t.devices[n].is_host
            and not any(p.iface for p in t.devices[n].ports.values())]
    end = time.monotonic() + wait
    while need and time.monotonic() < end:
        await asyncio.sleep(1.5)
        t = await topo()
        need = [n for n in need if not any(p.iface for p in t.devices[n].ports.values())]
    return t


async def _ping_pairs(pairs: list[list[str]], count: int = 2, timeout: float = 45.0, trace: bool = False) -> str:
    t = await topo()
    t = await _wait_dhcp(list({p[0] for p in pairs}), t)
    jobs: dict[str, list[tuple[str, str, str]]] = {}
    lines_err = []
    for src, dst in pairs:
        if src not in t.devices:
            lines_err.append(f"{src} -> {dst}: ERROR no device {src}")
            continue
        try:
            ip = await _resolve_ip(dst, t)
        except ValueError as e:
            lines_err.append(f"{src} -> {dst}: ERROR {e}")
            continue
        dev = t.devices[src]
        if trace:
            cmd = f"tracert {ip}" if dev.is_host else f"traceroute {ip}"
        else:
            cmd = f"ping -n {count} {ip}" if dev.is_host else f"ping {ip}"
        jobs.setdefault(src, []).append((dst, ip, cmd))
    results: dict[tuple[str, str], str] = {}
    retried: set[tuple[str, str]] = set()
    running: dict[str, tuple[str, str, str, str, float]] = {}  # src -> (jobid, dst, ip, cmd, started)
    end = time.monotonic() + timeout + 10 * max((len(v) for v in jobs.values()), default=1)

    async def start_next(src):
        if jobs.get(src):
            dst, ip, cmd = jobs[src].pop(0)
            jid = await call("startCmd", src, cmd)
            running[src] = (jid, dst, ip, cmd, time.monotonic())

    for src in list(jobs):
        await start_next(src)
    done_rx = TRACE_DONE if trace else PING_DONE
    per_job = 60.0 if trace else 8.0 + 4.0 * max(count, 5)
    while running and time.monotonic() < end:
        await asyncio.sleep(0.7)
        outs = await call("readCmds", [r[0] for r in running.values()], False)
        for src, (jid, dst, ip, cmd, started) in list(running.items()):
            out = outs.get(jid) or ""
            finished = bool(done_rx.search(out))
            if finished and not trace:
                await asyncio.sleep(0.3)  # let the RTT line arrive
                out = (await call("readCmds", [jid], False)).get(jid) or out
            if finished or time.monotonic() - started > per_job:
                await call("readCmds", [jid], True)
                summary = out if trace else _ping_summary(out)
                del running[src]
                if not trace and (src, dst) not in retried and (
                        summary.startswith("PARTIAL") or (summary.startswith("FAIL 0/") and "timed out" in summary)):
                    # cold paths lose echoes to ARP, retry once
                    retried.add((src, dst))
                    jobs[src].insert(0, (dst, ip, cmd))
                else:
                    if (src, dst) in retried:
                        summary += (" (on retry: first attempt lost to ARP warm-up)" if summary.startswith("OK")
                                    else " (also on retry)")
                    results[(src, dst)] = summary
                await start_next(src)
    for src, (jid, dst, *_rest) in running.items():
        await call("readCmds", [jid], True)
        results[(src, dst)] = "TIMEOUT"
    lines = []
    for src, dst in pairs:
        r = results.get((src, dst))
        if r is None:
            continue
        if trace:
            body = "\n".join(l for l in r.replace("\r", "").split("\n") if re.match(r"\s*\d+\s", l) or "Trac" in l)
            lines.append(f"{src} -> {dst}\n{body.strip() or r.strip()}")
        else:
            lines.append(f"{src} -> {dst}: {r}")
    return "\n".join(lines_err + lines)


@tool(ro=True)
async def pt_ping(pairs: list[list[str]], count: int = 2) -> str:
    """Ping from devices and report OK/PARTIAL/FAIL per pair. pairs: [[src, dst], ...] where dst is a device
    name or IP. Sources can be PCs/servers (Command Prompt) or routers/switches (IOS ping). Pairs from
    different sources run in parallel. A first-ping loss is usually ARP warm-up: count>=2 hides it."""
    return await _ping_pairs(pairs, count=count)


@tool(ro=True)
async def pt_ping_matrix(hosts: list[str] | None = None, count: int = 2) -> str:
    """All-pairs reachability between end hosts (default: every PC/laptop/server, max 10) as a matrix."""
    t = await topo()
    names = hosts or sorted(n for n, d in t.devices.items() if d.is_host and d.type in (8, 9, 18))[:10]
    if len(names) < 2:
        return "need at least 2 hosts"
    pairs = [[a, b] for a in names for b in names if a != b]
    text = await _ping_pairs(pairs, count=count, timeout=60)
    res = {}
    for line in text.split("\n"):
        m = re.match(r"(\S+) -> (\S+): (\w+)", line)
        if m:
            res[(m.group(1), m.group(2))] = {"OK": "ok", "PARTIAL": "part", "FAIL": "fail", "TIMEOUT": "t/o"}.get(m.group(3), "!")
    w = max(len(n) for n in names)
    head = " " * (w + 1) + " ".join(n[:6].ljust(6) for n in names)
    rows = [head] + [a.ljust(w) + " " + " ".join((res.get((a, b), ".") if a != b else "-").ljust(6) for b in names)
                     for a in names]
    ok = sum(1 for v in res.values() if v == "ok")
    return "\n".join(rows) + f"\n{ok}/{len(pairs)} reachable (part = partial, t/o = timeout)"


@tool(ro=True)
async def pt_traceroute(src: str, dst: str) -> str:
    """Traceroute (tracert on hosts) from a device to a device name or IP; returns the hop list."""
    return await _ping_pairs([[src, dst]], trace=True, timeout=60)


_PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")]


def _rfc1918(net: str) -> bool:
    n = ipaddress.ip_network(net, strict=False)
    return any(n.subnet_of(p) for p in _PRIVATE)


def _has_private(d: planner.Device) -> bool:
    return any(p.iface and _rfc1918(str(p.iface.network)) for p in d.ports.values())


async def _route_gaps(t: planner.Topology) -> list[str]:
    """Subnets each router has no route to (reads every routing table)."""
    issues: list[str] = []
    owners: dict[str, list[str]] = {}
    for d in t.devices.values():
        for p in d.ports.values():
            if p.iface and p.iface.network.prefixlen < 32 and t.is_l3(d.name, p.name) and p.up == 2:
                owners.setdefault(str(p.iface.network), []).append(d.name)
    routers = [d.name for d in t.devices.values() if d.on and (d.is_router or d.type in (16, 17))]
    outs = await asyncio.gather(*(call("cli", r, ["show ip route"], "exec") for r in routers), return_exceptions=True)
    nat = t.nat_inside()
    for r, res in zip(routers, outs):
        if isinstance(res, Exception):
            issues.append(f"{r}: could not read routing table ({res})")
            continue
        table = "\n".join(o for _, _, o in res["r"])
        if not table.strip():
            continue
        dynamic = re.search(r"^router (ospf|eigrp|rip)", t.configs.get(r, ""), re.M)
        if re.search(r"Gateway of last resort is (?!not set)\S+", table) and not dynamic:
            continue  # static stub router: its default route covers everything
        have = {m.group(1) for m in re.finditer(r"(\d+\.\d+\.\d+\.\d+)(/\d+)?\s", table)}
        missing = sorted(n for n in owners if n.split("/")[0] not in have)
        if nat and not _has_private(t.devices[r]):
            # routers outside the NAT edge don't need inside routes
            missing = [n for n in missing if not _rfc1918(n)]
        elif nat and dynamic:
            # the default route covers the public nets
            missing = [n for n in missing if _rfc1918(n)]
        for n in missing:
            where = ", ".join(sorted(set(owners[n])))
            issues.append(f"{r}: no route to {n} (connected on {where}; not advertised/redistributed?)")
    return issues


async def _converge(max_rounds: int = 8) -> str:
    """Fast-forward until every router has a route to every live subnet (or give up).
    Always fast-forwards past STP listening/learning first: new switch ports forward only after ~30s."""
    await call("fastForward", 3)
    for i in range(max_rounds):
        t = await topo(configs=True)
        gaps = await _route_gaps(t)
        if not gaps:
            return f"converged after {i} fast-forward round(s)"
        await call("fastForward", 2)
    return f"not converged after {max_rounds} rounds: " + "; ".join(gaps[:4])


@tool(ro=True)
async def pt_diagnose(deep: bool = False) -> str:
    """Find misconfigurations from the live state: duplicate IPs, mixed subnets on a segment, hosts without
    IP / wrong or missing gateway, cabled-but-down interfaces, powered-off devices, uncabled hosts.
    deep=true also reads every router's routing table and lists subnets each router cannot reach."""
    t = await topo(detail=True, configs=True)
    issues = planner.diagnose(t)
    if deep:
        issues += await _route_gaps(t)
    if not issues:
        return f"No issues found across {len(t.devices)} devices." + ("" if deep else " (deep=true also checks routing tables)")
    return f"{len(issues)} issue(s):\n" + "\n".join(f"- {i}" for i in issues)


@tool
async def pt_fast_forward(times: int = 1) -> str:
    """Fast-forward PT's realtime clock (speeds up STP, OSPF/EIGRP convergence, DHCP)."""
    await call("fastForward", max(1, min(times, 10)))
    return f"fast-forwarded x{times}"


@tool
async def pt_simulation(action: Literal["status", "simulation", "realtime", "reset", "forward", "back", "pdu", "frames"],
                        src: str | None = None, dst: str | None = None, steps: int = 1) -> str:
    """Simulation mode control. action: simulation|realtime (switch mode), pdu (simple ICMP PDU src->dst),
    forward/back (steps events), reset, frames (last events with status), status."""
    if action == "frames":
        rows = await call("frames", 30)
        return "\n".join(f"#{i} {s} -> {d} {ty} {st}" for i, s, d, ty, st in rows) or "(no events)"
    arg: Any = None
    act = action
    if action in ("simulation", "realtime"):
        act, arg = "mode", action == "simulation"
    elif action == "pdu":
        if not (src and dst):
            return "ERROR: pdu needs src and dst"
        await call("sim", "mode", True)
        r = await call("sim", "pdu", [src, dst])
        return f"PDU {src}->{dst} added (code {r}); step with action=forward, inspect with action=frames"
    elif action in ("forward", "back"):
        arg = steps
    r = await call("sim", act, arg)
    return f"mode={'simulation' if r['sim'] else 'realtime'} time={r['t']} events={r['frames']}"


@tool
async def pt_file(action: Literal["new", "open", "save", "save_as"], path: str | None = None) -> str:
    """Packet Tracer file operations on the open workspace: new, open <path>, save, save_as <path> (.pkt)."""
    if action == "new":
        return "new file" if await call("fileNew") else "PT refused (unsaved changes prompt?)"
    if action == "open":
        if not path:
            return "ERROR: path required"
        return f"open: {await call('fileOpen', str(Path(path).expanduser()), timeout=120)}"
    if action == "save_as":
        if not path:
            return "ERROR: path required"
        p = str(Path(path).expanduser())
        await call("fileSave", p, timeout=120)
        return f"saved {p}"
    return "saved" if await call("fileSave", None, timeout=120) else "save failed (never saved? use save_as)"


@mcp.tool(annotations=RO, structured_output=False)
def pkt_inspect(path: str, configs: bool = False) -> str:
    """Read a .pkt/.pka file WITHOUT Packet Tracer: devices, models, IPs, links, and (configs=true)
    every device's running-config. Good for grading or understanding a lab file."""
    try:
        return pkt.summary_text(pkt.inspect(path), configs=configs)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


@mcp.tool(annotations=ToolAnnotations(open_world_hint=False), structured_output=False)
def pkt_convert(src: str, dst: str) -> str:
    """Convert .pkt <-> .xml without Packet Tracer (decrypt to editable XML, or re-encrypt XML into a .pkt)."""
    try:
        s, d = Path(src).expanduser(), Path(dst).expanduser()
        if s.suffix.lower() in (".pkt", ".pka"):
            d.write_text(pkt.read_xml(s))
            return f"wrote XML {d} ({d.stat().st_size // 1024} KB)"
        pkt.write_pkt(s.read_text(), d)
        return f"wrote {d}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


@mcp.tool(annotations=ToolAnnotations(open_world_hint=False), structured_output=False)
def pkt_export_configs(path: str, out_dir: str) -> str:
    """Write every device's running-config from a .pkt file to <out_dir>/<device>.txt."""
    try:
        info = pkt.inspect(path)
        out = Path(out_dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        n = 0
        for d in info["devices"]:
            if d["running_config"]:
                (out / f"{d['name']}.txt").write_text("\n".join(d["running_config"]) + "\n")
                n += 1
        return f"wrote {n} configs to {out}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


@mcp.tool(annotations=RO, structured_output=False)
def net_subnet(cidr: str) -> str:
    """Subnet facts for an address/prefix: network, mask, wildcard, broadcast, host range, count, class."""
    try:
        return "\n".join(f"{k}: {v}" for k, v in netcalc.subnet_info(cidr).items())
    except ValueError as e:
        return f"ERROR: {e}"


@mcp.tool(annotations=RO, structured_output=False)
def net_vlsm(pool: str, needs: dict[str, int]) -> str:
    """VLSM plan: needs = {'Sales': 50, 'Eng': 25, 'R1-R2': 2}. Largest first; returns subnet, mask,
    wildcard, gateway (first host), host range and broadcast per segment."""
    try:
        rows = [a.row() for a in netcalc.vlsm(pool, needs)]
        return netcalc.table(rows, ["name", "need", "subnet", "mask", "wildcard", "gateway", "range", "broadcast", "usable"])
    except ValueError as e:
        return f"ERROR: {e}"


@mcp.tool(annotations=RO, structured_output=False)
def net_summarize(networks: list[str]) -> str:
    """Route summarization: exact collapsed set and the single covering supernet (+ how much extra it covers)."""
    try:
        return "\n".join(f"{k}: {v}" for k, v in netcalc.summarize(networks).items())
    except ValueError as e:
        return f"ERROR: {e}"


@mcp.tool(annotations=RO, structured_output=False)
def net_split(cidr: str, count: int | None = None, new_prefix: int | None = None) -> str:
    """Split a block into `count` equal subnets or into /new_prefix subnets (first 64 shown)."""
    try:
        subs = netcalc.split(cidr, new_prefix=new_prefix, count=count)
        return "\n".join(subs[:64]) + (f"\n... {len(subs)} total" if len(subs) > 64 else "")
    except (ValueError, TypeError) as e:
        return f"ERROR: {e}"


@mcp.tool(annotations=RO, structured_output=False)
def ios_features() -> str:
    """List IOS config generators and their parameters (used by ios_generate / pt_apply_feature)."""
    return configgen.feature_help()


@mcp.tool(annotations=RO, structured_output=False)
def ios_generate(feature: str, params: dict) -> str:
    """Generate IOS config lines for a feature without applying them. See ios_features for names/params.
    e.g. ('dhcp_pool', {'name':'LAN','network':'192.168.1.0/24','dns':'8.8.8.8','exclude_first':10})."""
    try:
        return "\n".join(configgen.generate(feature, params))
    except (ValueError, TypeError) as e:
        return f"ERROR: {e}"


@mcp.tool(annotations=RO, structured_output=False)
def pt_catalog(category: str | None = None, modules: bool = False) -> str:
    """Device models PT 9 offers (exact names for pt_add_devices) with ports, slots and usage notes.
    category: router | switch | l3-switch | host | firewall | ap | wireless-router ... modules=true lists modules."""
    if modules:
        return "\n".join(f"{m}: {n}" for m, n in catalog.MODULE_NOTES.items()) + \
            "\nAll modules: " + ", ".join(sorted(catalog.MODULE_TYPES))
    text = catalog.catalog_text(category)
    cables = "\nCables: " + "; ".join(f"{k} = {v}" for k, v in catalog.CABLES.items())
    return (text or f"no models in category {category}") + cables


_API: dict | None = None


@mcp.tool(annotations=RO, structured_output=False)
def pt_api_lookup(query: str, limit: int = 40) -> str:
    """Search Packet Tracer's IPC/scripting API (6000+ methods, 374 classes) for use with pt_run_js.
    query: a class name ('TerminalLine'), a method fragment ('Vlan', 'ospf'), or 'Class.method'."""
    global _API
    if _API is None:
        with resources.files("ptmcp").joinpath("data/pt_api.json.gz").open("rb") as fh:
            _API = json.loads(gzip.decompress(fh.read()))
    q = query.strip()
    if q in _API:
        c = _API[q]
        head = f"{q}: {c['brief']}" + (f" (inherits {', '.join(c['bases'])})" if c["bases"] else "")
        return head + "\n" + "\n".join(f"  {r} {s}" + (f"  - {d}" if d else "") for r, s, d in c["methods"][:150])
    cls_q, _, meth_q = q.partition(".")
    hits = []
    for cname, c in _API.items():
        if meth_q and cls_q.lower() not in cname.lower():
            continue
        needle = (meth_q or q).lower()
        if not meth_q and needle in cname.lower():
            hits.append(f"[class] {cname}: {c['brief'][:100]}")
        for r, s, d in c["methods"]:
            if needle in s.lower():
                hits.append(f"{cname}.{s} -> {r}" + (f"  - {d[:90]}" if d else ""))
    seen, uniq = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    if not uniq:
        return f"no match for {query}"
    return "\n".join(uniq[:limit]) + (f"\n... {len(uniq)} matches" if len(uniq) > limit else "")


@tool
async def pt_run_js(code: str, timeout: float = 60) -> str:
    """Escape hatch: run JavaScript inside PT's script engine and return the JSON result. The code is a
    function body: use `return`. Globals: ipc (IPC root: ipc.network(), ipc.appWindow(), ipc.simulation()),
    PTM (this server's helpers: PTM.dev(name), PTM.snapshot(), PTM.cli(name, lines, mode)...).
    Example: return ipc.network().getDevice('R1').getPort('GigabitEthernet0/0').getMacAddress();"""
    v = await bridge.run(code, timeout)
    return json.dumps(v, indent=1) if not isinstance(v, str) else v


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler()])
    mcp.run()


if __name__ == "__main__":
    main()
