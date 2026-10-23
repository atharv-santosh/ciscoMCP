"""Read and write Packet Tracer .pkt/.pka files without Packet Tracer running.

File format (PT 7+), as documented by the pka2xml project:
  stage 1  reverse the bytes and XOR each with (len - i*len) & 0xFF
  stage 2  Twofish-EAX, key = 0x89 * 16, nonce = 0x10 * 16 (16-byte tag at the end)
  stage 3  XOR each byte with (len - i) & 0xFF
  stage 4  4-byte big-endian uncompressed size + zlib stream of the XML
"""

from __future__ import annotations

import ipaddress
import re
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

from .twofish import EAX

KEY = bytes([0x89]) * 16
NONCE = bytes([0x10]) * 16


def decrypt(data: bytes) -> bytes:
    n = len(data)
    stage1 = bytes(data[n - 1 - i] ^ ((n - i * n) & 0xFF) for i in range(n))
    stage2 = EAX(KEY).decrypt(NONCE, stage1)
    m = len(stage2)
    stage3 = bytes(stage2[i] ^ ((m - i) & 0xFF) for i in range(m))
    size = int.from_bytes(stage3[:4], "big")
    xml = zlib.decompress(stage3[4:])
    if size and len(xml) != size:
        raise ValueError(f"size mismatch: header {size}, got {len(xml)}")
    return xml


def encrypt(xml: bytes) -> bytes:
    stage3 = len(xml).to_bytes(4, "big") + zlib.compress(xml, 6)
    m = len(stage3)
    stage2 = bytes(stage3[i] ^ ((m - i) & 0xFF) for i in range(m))
    stage1 = EAX(KEY).encrypt(NONCE, stage2)
    n = len(stage1)
    out = bytearray(n)
    for i in range(n):
        out[n - 1 - i] = stage1[i] ^ ((n - i * n) & 0xFF)
    return bytes(out)


def read_xml(path: str | Path) -> str:
    return decrypt(Path(path).expanduser().read_bytes()).decode("utf-8", "replace")


def write_pkt(xml: str | bytes, path: str | Path) -> Path:
    p = Path(path).expanduser()
    p.write_bytes(encrypt(xml.encode() if isinstance(xml, str) else xml))
    return p


_IF_RE = re.compile(r"^interface (\S+)")
_IP_RE = re.compile(r"^\s*ip address (\d+\.\d+\.\d+\.\d+) (\d+\.\d+\.\d+\.\d+)")


def _cfg_lines(engine: ET.Element, tag: str) -> list[str]:
    el = engine.find(tag)
    if el is None:
        return []
    return [(l.text or "") for l in el.findall("LINE")]


def _iface_ips(cfg: list[str]) -> dict[str, str]:
    out, cur = {}, None
    for line in cfg:
        m = _IF_RE.match(line)
        if m:
            cur = m.group(1)
            continue
        if line and not line.startswith(" "):
            cur = None
        m = _IP_RE.match(line)
        if cur and m:
            plen = ipaddress.IPv4Network(f"0.0.0.0/{m.group(2)}").prefixlen
            out[cur] = f"{m.group(1)}/{plen}"
    return out


def _port_elems(module: ET.Element | None):
    if module is None:
        return
    for child in module:
        if child.tag == "PORT":
            yield child
        elif child.tag == "SLOT":
            for m in child.findall("MODULE"):
                yield from _port_elems(m)
        elif child.tag == "MODULE":
            yield from _port_elems(child)


def parse(xml: str) -> dict:
    root = ET.fromstring(xml)
    net = root.find("NETWORK")
    devices = []
    for d in net.find("DEVICES") if net is not None and net.find("DEVICES") is not None else []:
        e = d.find("ENGINE")
        typ = e.find("TYPE")
        lg = d.find("WORKSPACE/LOGICAL")
        run = _cfg_lines(e, "RUNNINGCONFIG")
        start = _cfg_lines(e, "STARTUPCONFIG")
        rec = {
            "ref": e.findtext("SAVE_REF_ID") or "",
            "name": e.findtext("NAME"), "type": typ.text if typ is not None else "",
            "model": (typ.attrib.get("model") if typ is not None else "") or "",
            "power": e.findtext("POWER") == "true",
            "x": _num(lg.findtext("X") if lg is not None else None),
            "y": _num(lg.findtext("Y") if lg is not None else None),
            "running_config": run, "startup_config": start,
            "ips": _iface_ips(run),
        }
        host_ips = []
        for p in _port_elems(e.find("MODULE") if not run else None):   # IOS devices: IPs come from the config
            ip, sub = p.findtext("IP") or "", p.findtext("SUBNET") or ""
            if not ip and p.findtext("PORT_DHCP_ENABLE") == "true":
                host_ips.append({"ip": "(no lease saved)", "gateway": "", "dns": "", "dhcp": True})
            elif ip:
                plen = ipaddress.IPv4Network(f"0.0.0.0/{sub}").prefixlen if sub else 32
                host_ips.append({"ip": f"{ip}/{plen}", "gateway": p.findtext("PORT_GATEWAY") or "",
                                 "dns": p.findtext("PORT_DNS") or "",
                                 "dhcp": p.findtext("PORT_DHCP_ENABLE") == "true"})
        if host_ips:
            rec["host_ips"] = host_ips
        gw = e.findtext("GATEWAY") or ""
        if gw:
            rec["gateway"] = gw
        devices.append(rec)
    by_ref = {d["ref"]: d["name"] for d in devices if d["ref"]}
    all_names = [d["name"] for d in devices]
    devices = [d for d in devices if d["type"] != "Power Distribution Device"]  # PT 9 adds one, off-canvas
    links = []
    for l in net.find("LINKS") if net is not None and net.find("LINKS") is not None else []:
        c = l.find("CABLE")
        if c is None:
            continue
        ports = [p.text for p in c.findall("PORT")]
        a, b = c.findtext("FROM") or "", c.findtext("TO") or ""
        if a in by_ref or b in by_ref:          # PT 9: endpoints are SAVE_REF_IDs
            a, b = by_ref.get(a, a), by_ref.get(b, b)
        else:                                    # PT 7/8: endpoints are device indexes
            try:
                a, b = all_names[int(a)], all_names[int(b)]
            except (ValueError, IndexError):
                pass
        links.append({"a": a, "a_port": ports[0] if ports else "", "b": b,
                      "b_port": ports[1] if len(ports) > 1 else "",
                      "media": l.findtext("TYPE"), "cable": _cable_name(c.findtext("TYPE") or l.findtext("TYPE") or "")})
    return {"version": root.findtext("VERSION"), "devices": devices, "links": links,
            "has_script_module": root.find("SCRIPT_MODULE") is not None and len(root.find("SCRIPT_MODULE")) > 0}


_CABLES = {"StraightThrough": "straight", "CrossOver": "cross", "Serial": "serial", "Fiber": "fiber",
           "Console": "console", "Phone": "phone", "Coaxial": "coaxial", "RollOver": "console", "Octal": "octal"}


def _cable_name(raw: str) -> str:
    raw = raw[1:] if len(raw) > 1 and raw[0] == "e" and raw[1].isupper() else raw   # eStraightThrough
    return _CABLES.get(raw, raw.lower() or "?")


def _num(v):
    try:
        return round(float(v))
    except (TypeError, ValueError):
        return None


def inspect(path: str | Path) -> dict:
    return parse(read_xml(path))


def summary_text(info: dict, configs: bool = False) -> str:
    out = [f"Packet Tracer file version {info['version']}: {len(info['devices'])} devices, {len(info['links'])} links"]
    for d in info["devices"]:
        line = f"- {d['name']} ({d['model'] or d['type']})"
        if not d["power"]:
            line += " [off]"
        addrs = [f"{k}={v}" for k, v in d["ips"].items()]
        for h in d.get("host_ips", []):
            addrs.append(("dhcp " if h["dhcp"] else "") + h["ip"] + (f" gw {h['gateway']}" if h["gateway"] else ""))
        if addrs:
            line += ": " + ", ".join(addrs)
        out.append(line)
    out.append("Links:")
    for l in info["links"]:
        out.append(f"- {l['a']}:{l['a_port']} <-> {l['b']}:{l['b_port']} ({l['cable']})")
    if configs:
        for d in info["devices"]:
            if d["running_config"]:
                body = [ln for ln in d["running_config"] if ln.strip() not in ("!", "")]
                out.append(f"\n### {d['name']} running-config\n" + "\n".join(body))
    return "\n".join(out)
