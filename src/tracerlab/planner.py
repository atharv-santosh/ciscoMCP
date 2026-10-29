"""Topology logic on top of PTM.snapshot(): segments, auto addressing, routing, diagnostics.

snapshot: devices [{n, m, t, x, y, on, ports: [[name, ip/len, peerDev, peerPort, up, portType, extra?]]}]
and links [[devA, portA, devB, portB, cable]]. With running configs the segments are VLAN aware.
"""

from __future__ import annotations

import ipaddress
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field

from . import configgen as cg
from .catalog import CLI_TYPES, HOST_TYPES, L2_TYPES, L3_SWITCH_TYPES, TYPE_NAMES
from .netcalc import Allocator, prefix_for_hosts

UP_TEXT = {0: "down", 1: "up/proto-down", 2: "up"}


@dataclass
class Port:
    dev: str
    name: str
    ip: str
    peer_dev: str
    peer_port: str
    up: int
    ptype: int
    extra: dict = field(default_factory=dict)

    @property
    def iface(self):
        return ipaddress.ip_interface(self.ip) if self.ip else None


@dataclass
class Device:
    name: str
    model: str
    type: int
    x: int
    y: int
    on: bool
    ports: dict[str, Port]
    gw: str = ""
    dhcp: bool = False

    @property
    def kind(self) -> str:
        return TYPE_NAMES.get(self.type, str(self.type))

    @property
    def is_router(self) -> bool:
        return self.type == 0

    @property
    def is_host(self) -> bool:
        return self.type in HOST_TYPES

    @property
    def is_l2(self) -> bool:
        return self.type in L2_TYPES or self.type in L3_SWITCH_TYPES

    @property
    def has_cli(self) -> bool:
        return self.type in CLI_TYPES


_IF_HDR = re.compile(r"^interface (\S+)")
_IP_LINE = re.compile(r"^ ip address (\d+\.\d+\.\d+\.\d+) (\d+\.\d+\.\d+\.\d+)")


def _vlan_list(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.add(int(part))
    return out


def parse_interfaces(cfg: str) -> dict[str, dict]:
    """Per-interface L2/L3 facts from an IOS running-config.
    {name: {mode: access|trunk|routed|None, vlan, native, allowed (set|None), ip, dot1q, dot1q_native, group}}"""
    out: dict[str, dict] = {}
    cur = None
    for line in cfg.splitlines():
        m = _IF_HDR.match(line)
        if m:
            cur = out.setdefault(m.group(1), {"mode": None, "vlan": 1, "native": 1, "allowed": None, "ip": "",
                                              "dot1q": None, "dot1q_native": False, "group": None, "vips": []})
            continue
        if not line.startswith(" "):
            cur = None
        if cur is None:
            continue
        t = line.strip()
        if t == "no switchport":
            cur["mode"] = "routed"
        elif t.startswith("switchport mode "):
            mode = t.split()[2]
            cur["mode"] = "trunk" if mode == "trunk" else ("access" if mode == "access" else cur["mode"])
        elif t.startswith("switchport access vlan "):
            cur["vlan"] = int(t.split()[3])
        elif t.startswith("switchport trunk native vlan "):
            cur["native"] = int(t.split()[4])
        elif t.startswith("switchport trunk allowed vlan "):
            rest = t[len("switchport trunk allowed vlan "):]
            if rest.startswith("add "):
                cur["allowed"] = (cur["allowed"] or set()) | _vlan_list(rest[4:])
            elif rest.startswith("remove ") and cur["allowed"] is not None:
                cur["allowed"] -= _vlan_list(rest[7:])
            elif rest in ("all",):
                cur["allowed"] = None
            elif rest == "none":
                cur["allowed"] = set()
            else:
                cur["allowed"] = _vlan_list(rest)
        elif t.startswith("encapsulation dot1Q ") or t.startswith("encapsulation dot1q "):
            parts = t.split()
            cur["dot1q"] = int(parts[2])
            cur["dot1q_native"] = len(parts) > 3 and parts[3] == "native"
        elif t.startswith("channel-group "):
            cur["group"] = int(t.split()[1])
        elif re.match(r"(standby|vrrp|glbp) \d+ ip \d", t):     # HSRP/VRRP/GLBP vip
            cur["vips"].append(t.split()[3])
        else:
            mi = _IP_LINE.match(line)
            if mi:
                plen = ipaddress.IPv4Network(f"0.0.0.0/{mi.group(2)}").prefixlen
                cur["ip"] = f"{mi.group(1)}/{plen}"
    return out


class Topology:
    def __init__(self, snap: dict, configs: dict[str, str] | None = None):
        self.devices: dict[str, Device] = {}
        for d in snap.get("devices", []):
            ports = {}
            for row in d.get("ports", []):
                extra = row[6] if len(row) > 6 and isinstance(row[6], dict) else {}
                ports[row[0]] = Port(d["n"], row[0], row[1] or "", row[2] or "", row[3] or "",
                                     int(row[4] or 0), int(row[5]) if row[5] is not None else -1, extra)
            self.devices[d["n"]] = Device(d["n"], d.get("m", ""), int(d.get("t", -1)), d.get("x", 0), d.get("y", 0),
                                          bool(d.get("on", 1)), ports, d.get("gw", ""),
                                          bool(d.get("dhcp")))
        self.links = [tuple(l) for l in snap.get("links", [])]
        self.configs = configs or {}
        self.ifcfg: dict[str, dict[str, dict]] = {n: parse_interfaces(c) for n, c in self.configs.items()
                                                 if n in self.devices}
        for n, ifs in self.ifcfg.items():
            dev = self.devices[n]
            if not dev.is_l2:
                continue
            for name, c in ifs.items():
                # the snapshot misses IPs on routed switch ports
                if c["ip"] and name in dev.ports and not dev.ports[name].ip:
                    dev.ports[name].ip = c["ip"]
                elif c["ip"] and name not in dev.ports:
                    dev.ports[name] = Port(n, name, c["ip"], "", "", 2, -1)

    def _cfg(self, dev: str, port: str) -> dict:
        return self.ifcfg.get(dev, {}).get(port) or {}

    def nat_inside(self) -> bool:
        return any("ip nat inside source" in c for c in self.configs.values())

    def _switchport(self, dev: str, port: str) -> bool:
        """True when this port on an L2/L3 switch bridges frames (not a routed port)."""
        d = self.devices[dev]
        if not d.is_l2:
            return False
        if port.startswith("Vlan"):
            return False
        c = self._cfg(dev, port)
        if c.get("mode") == "routed":
            return False
        g = c.get("group")
        if g is not None and self._cfg(dev, f"Port-channel{g}").get("mode") == "routed":
            return False
        return True

    def is_l3(self, dev: str, port: str) -> bool:
        """A routing interface: router/ASA port, or an SVI / routed port on an L3 switch."""
        d = self.devices[dev]
        if not d.has_cli:
            return False
        if not d.is_l2:
            return True
        return d.type in L3_SWITCH_TYPES and not self._switchport(dev, port)

    def _port_mode(self, dev: str, port: str) -> dict:
        """Effective switchport mode: explicit config, the port-channel's, or DTP against the peer."""
        c = dict(self._cfg(dev, port)) or {"mode": None, "vlan": 1, "native": 1, "allowed": None}
        g = c.get("group")
        if g is not None:
            pc = self._cfg(dev, f"Port-channel{g}")
            if pc and (c.get("mode") is None or pc.get("mode") == "trunk"):
                c.update({k: pc[k] for k in ("mode", "vlan", "native", "allowed")})
        if c.get("mode") is None:
            p = self.devices[dev].ports.get(port)
            peer_c = self._cfg(p.peer_dev, p.peer_port) if p and p.peer_dev else {}
            c["mode"] = "trunk" if peer_c.get("mode") == "trunk" else "access"
        return c

    def text(self, detail: bool = False) -> str:
        if not self.devices:
            return "Canvas is empty."
        by_kind = defaultdict(list)
        for d in self.devices.values():
            by_kind[d.kind].append(d)
        out = [f"{len(self.devices)} devices, {len(self.links)} links"]
        for kind in sorted(by_kind):
            for d in sorted(by_kind[kind], key=lambda d: d.name):
                head = f"{d.name} [{d.model} {kind}] @({d.x},{d.y})" + ("" if d.on else " POWER-OFF")
                if d.gw:
                    head += f" gw {d.gw}"
                rows = []
                for p in d.ports.values():
                    if not (p.ip or p.peer_dev or detail):
                        continue
                    s = f"  {p.name}"
                    if p.ip:
                        s += f" {p.ip}"
                    if p.extra.get("dhcp") or (d.dhcp and d.is_host and p.peer_dev):
                        s += " (dhcp)"
                    if p.peer_dev:
                        s += f" -> {p.peer_dev}:{p.peer_port}"
                    if p.peer_dev or p.ip:
                        s += f" [{UP_TEXT.get(p.up, p.up)}]"
                    if p.extra.get("sw"):
                        s += f" {p.extra['sw']}"
                    if p.extra.get("desc"):
                        s += f" \"{p.extra['desc']}\""
                    rows.append(s)
                if not rows and not detail:
                    free = sum(1 for p in d.ports.values() if not p.peer_dev)
                    head += f" ({free} free ports)"
                out.append(head)
                out.extend(rows)
        return "\n".join(out)

    def segments(self) -> list[dict]:
        """L3 broadcast domains: {endpoints: [(device, interface)], switches: [names]}.
        Walks switches per VLAN; without configs every switch port is an access port in VLAN 1."""
        adj: dict[tuple[str, str], tuple[str, str]] = {}
        for a, ap, b, bp, *_ in self.links:
            adj[(a, ap)] = (b, bp)
            adj[(b, bp)] = (a, ap)

        attach: dict[tuple[str, str], list[tuple[str, int | None]]] = {}
        by_phys: dict[tuple[str, str], dict[int | None, tuple[str, str]]] = defaultdict(dict)
        svis: dict[tuple[str, int], tuple[str, str]] = {}
        for d in self.devices.values():
            subifs = {p.split(".")[0] for p in d.ports if "." in p}
            for p in d.ports.values():
                if d.is_l2:
                    if p.name.startswith("Vlan") and p.name[4:].isdigit():
                        if p.ip or d.type in L3_SWITCH_TYPES:   # SVI
                            ep = (d.name, p.name)
                            attach[ep] = []
                            svis[(d.name, int(p.name[4:]))] = ep
                        continue
                    if not self.is_l3(d.name, p.name):
                        continue
                if p.name.startswith("Port-channel"):
                    g = p.name[len("Port-channel"):]
                    members = [m for m, c in self.ifcfg.get(d.name, {}).items() if str(c.get("group")) == g]
                    if members:
                        attach[(d.name, p.name)] = [(m, None) for m in members]
                        for m in members:
                            by_phys[(d.name, m)][None] = (d.name, p.name)
                    continue
                if "." in p.name:
                    phys = p.name.split(".")[0]
                    c = self._cfg(d.name, p.name)
                    tag = None if c.get("dot1q_native") else c.get("dot1q")
                    attach[(d.name, p.name)] = [(phys, tag)]
                    by_phys[(d.name, phys)][tag] = (d.name, p.name)
                    continue
                if p.name in subifs and not p.ip:
                    continue  # parent of subinterfaces: not an endpoint itself
                if (d.name, p.name) in by_phys and None in by_phys[(d.name, p.name)]:
                    continue  # channel member
                if self._cfg(d.name, p.name).get("group") is not None and not d.is_l2:
                    continue
                attach[(d.name, p.name)] = [(p.name, None)]
                by_phys[(d.name, p.name)][None] = (d.name, p.name)

        def arrive(dev: str, port: str, tag: int | None):
            """Where a frame sent onto the wire lands: a (switch, vlan) node or an endpoint."""
            if self._switchport(dev, port):
                c = self._port_mode(dev, port)
                if c["mode"] == "trunk":
                    v = c["native"] if tag is None else tag
                    if c["allowed"] is not None and v not in c["allowed"]:
                        return None
                    return ("sw", (dev, v))
                if tag is not None and tag != c["vlan"]:
                    return None
                return ("sw", (dev, c["vlan"]))
            ep = by_phys.get((dev, port), {}).get(tag)
            return ("ep", ep) if ep else None

        def neighbours(node):
            kind, key = node
            if kind == "ep":
                if key in svis.values():
                    yield ("sw", (key[0], int(key[1][4:])))
                for phys, tag in attach.get(key, []):
                    peer = adj.get((key[0], phys))
                    if peer:
                        nxt = arrive(peer[0], peer[1], tag)
                        if nxt:
                            yield nxt
                return
            dev, v = key
            if (dev, v) in svis:
                yield ("ep", svis[(dev, v)])
            for p in self.devices[dev].ports.values():
                peer = adj.get((dev, p.name))
                if not peer or not self._switchport(dev, p.name):
                    continue
                c = self._port_mode(dev, p.name)
                if c["mode"] == "trunk":
                    if c["allowed"] is not None and v not in c["allowed"]:
                        continue
                    tag = None if v == c["native"] else v
                elif c["vlan"] == v:
                    tag = None
                else:
                    continue
                nxt = arrive(peer[0], peer[1], tag)
                if nxt:
                    yield nxt

        seen: set = set()
        segs = []
        for ep in attach:
            start = ("ep", ep)
            if start in seen:
                continue
            seen.add(start)
            q = deque([start])
            eps, switches = [], []
            while q:
                node = q.popleft()
                if node[0] == "ep":
                    eps.append(node[1])
                elif node[1][0] not in switches:
                    switches.append(node[1][0])
                for nxt in neighbours(node):
                    if nxt not in seen:
                        seen.add(nxt)
                        q.append(nxt)
            if len(eps) == 1 and not switches and not self.port(*eps[0]).ip and \
                    not any(adj.get((eps[0][0], ph)) for ph, _ in attach[eps[0]]):
                continue
            segs.append({"endpoints": eps, "switches": switches})
        return segs

    def port(self, dev: str, port: str) -> Port:
        return self.devices[dev].ports[port]


@dataclass
class Plan:
    subnets: list[dict]
    device_cmds: dict[str, list[str]]
    host_cfg: dict[str, dict]
    notes: list[str]

    def text(self) -> str:
        out = ["Subnet plan:"]
        for s in self.subnets:
            members = ", ".join(f"{d}:{p}={ip}" for d, p, ip in s["members"])
            out.append(f"  {s['subnet']:<18} {s['kind']:<4} {members}")
        if self.notes:
            out.append("Notes: " + " | ".join(self.notes))
        return "\n".join(out)


def auto_address(topo: Topology, lan_pool: str = "192.168.0.0/16", p2p_pool: str = "10.0.0.0/24",
                 lan_prefix: int | None = 24, routing: str | None = "ospf", dhcp: bool = False,
                 dns: str | None = None, overwrite: bool = False, host_offset: int = 10,
                 ospf_area: int = 0, eigrp_as: int = 100, loopbacks: bool = False) -> Plan:
    """Address every L3 segment and generate routing.

    LAN segments (with hosts) draw from lan_pool, router-only segments are p2p
    /30s from p2p_pool. Existing addresses are kept unless overwrite=True.
    routing: ospf | eigrp | rip | static | none
    """
    lan_alloc, p2p_alloc = Allocator(lan_pool), Allocator(p2p_pool)
    segs = topo.segments()
    if not overwrite:
        for d in topo.devices.values():
            for p in d.ports.values():
                if p.iface:
                    for a in (lan_alloc, p2p_alloc):
                        if p.iface.network.overlaps(a.pool):
                            a.reserve(p.iface.network)
    if lan_pool.startswith("192.168.") and lan_prefix == 24:
        lan_alloc.reserve("192.168.0.0/24")  # start LANs at 192.168.1.0

    subnets, device_cmds, host_cfg, notes = [], defaultdict(list), {}, []
    router_ifaces: dict[str, list[tuple[str, ipaddress.IPv4Interface, bool]]] = defaultdict(list)  # dev -> (port, iface, is_lan)
    seg_records = []

    for seg in segs:
        eps = seg["endpoints"]
        routers = [(d, p) for d, p in eps if topo.is_l3(d, p)]
        hosts = [(d, p) for d, p in eps if topo.devices[d].is_host]
        if not routers and not hosts:
            continue
        existing = [topo.port(d, p).iface for d, p in eps if topo.port(d, p).iface]
        if existing and not overwrite:
            net = existing[0].network
            kind = "kept"
        else:
            if hosts:
                if lan_prefix:
                    plen = lan_prefix
                else:
                    plen = min(prefix_for_hosts(max(len(eps) * 2, 14)), 30)
                net = lan_alloc.take(plen)
                kind = "LAN"
            elif len(routers) >= 2:
                net = p2p_alloc.take(30 if len(routers) == 2 else prefix_for_hosts(len(routers)))
                kind = "P2P"
            else:
                notes.append(f"{routers[0][0]}:{routers[0][1]} is linked only to switches with no hosts; skipped")
                continue
        members = []
        used = {topo.port(d, p).iface.ip for d, p in eps if topo.port(d, p).iface and not overwrite}
        next_router = 1
        gateway = None
        for d, p in sorted(routers):
            pt = topo.port(d, p)
            if pt.iface and not overwrite and pt.iface.network == net:
                iface = pt.iface
            else:
                while net[next_router] in used:
                    next_router += 1
                iface = ipaddress.ip_interface(f"{net[next_router]}/{net.prefixlen}")
                used.add(iface.ip)
                device_cmds[d] += cg.interface(p, str(iface))
            gateway = gateway or str(iface.ip)
            router_ifaces[d].append((p, iface, bool(hosts)))
            members.append((d, p, str(iface)))
        if hosts and dhcp and routers and kind != "kept":
            r0 = sorted(routers)[0][0]
            device_cmds[r0] += cg.dhcp_pool(f"LAN-{net.network_address}", str(net), gateway, dns,
                                           exclude_first=min(host_offset - 1, net.num_addresses - 3))
        nh = host_offset
        for d, p in sorted(hosts):
            pt = topo.port(d, p)
            if pt.iface and not overwrite and pt.iface.network == net:
                members.append((d, p, str(pt.iface)))
                continue
            if dhcp and routers:
                host_cfg[d] = {"port": p, "dhcp": True}
                members.append((d, p, "dhcp"))
                continue
            while net[nh] in used:
                nh += 1
            if nh >= net.num_addresses - 1:
                notes.append(f"{net} too small for {d}")
                continue
            ip = net[nh]
            used.add(ip)
            host_cfg[d] = {"port": p, "ip": str(ip), "mask": str(net.netmask), "gw": gateway or "", "dns": dns or ""}
            members.append((d, p, f"{ip}/{net.prefixlen}"))
            nh += 1
        rec = {"subnet": str(net), "kind": kind, "members": members, "gateway": gateway,
               "routers": [d for d, _ in routers], "has_hosts": bool(hosts)}
        subnets.append(rec)
        seg_records.append(rec)

    routers = sorted(router_ifaces)
    if loopbacks:
        lo_alloc = Allocator("172.31.255.0/24")
        for i, r in enumerate(routers):
            net = lo_alloc.take(32)
            device_cmds[r] += cg.loopback(0, f"{net.network_address}/32")
            router_ifaces[r].append(("Loopback0", ipaddress.ip_interface(f"{net.network_address}/32"), True))
    rid = {r: f"{i + 1}.{i + 1}.{i + 1}.{i + 1}" for i, r in enumerate(routers)}
    routing = (routing or "none").lower()
    for r in routers:
        if routing != "none" and topo.devices[r].type in L3_SWITCH_TYPES:
            device_cmds[r].insert(0, "ip routing")
        nets = sorted({str(i.network) for _, i, _ in router_ifaces[r]})
        lan_ifs = [p for p, _, is_lan in router_ifaces[r] if is_lan and not p.startswith("Loopback")]
        if routing == "ospf":
            device_cmds[r] += cg.ospf(1, nets, ospf_area, router_id=rid[r], passive=lan_ifs)
        elif routing == "eigrp":
            device_cmds[r] += cg.eigrp(eigrp_as, nets, router_id=rid[r], passive=lan_ifs)
        elif routing == "rip":
            device_cmds[r] += cg.rip(nets, passive=lan_ifs)
    if routing == "static":
        for r, routes in static_routes(router_ifaces).items():
            device_cmds[r] += cg.static_routes(routes)
    return Plan(subnets, dict(device_cmds), host_cfg, notes)


def static_routes(router_ifaces: dict[str, list]) -> dict[str, list[dict]]:
    """Shortest-path static routes for every remote subnet, next hop = neighbour IP."""
    net_routers: dict[str, list[tuple[str, ipaddress.IPv4Interface]]] = defaultdict(list)
    for r, ifs in router_ifaces.items():
        for _, iface, _ in ifs:
            net_routers[str(iface.network)].append((r, iface))
    adj: dict[str, list[tuple[str, str]]] = defaultdict(list)   # r -> [(neighbor, neighbor_ip)]
    for net, members in net_routers.items():
        for r1, _ in members:
            for r2, i2 in members:
                if r1 != r2:
                    adj[r1].append((r2, str(i2.ip)))
    out: dict[str, list[dict]] = {}
    for src in router_ifaces:
        first_hop = {src: None}
        q = deque([src])
        while q:
            cur = q.popleft()
            for nb, nb_ip in adj[cur]:
                if nb not in first_hop:
                    first_hop[nb] = nb_ip if cur == src else first_hop[cur]
                    q.append(nb)
        connected = {str(i.network) for _, i, _ in router_ifaces[src]}
        routes = []
        for net, members in sorted(net_routers.items()):
            if net in connected:
                continue
            hops = [first_hop[r] for r, _ in members if first_hop.get(r)]
            if hops:
                routes.append({"dest": net, "via": hops[0]})
        out[src] = routes
    return out


def diagnose(topo: Topology) -> list[str]:
    issues: list[str] = []
    seen_ip: dict[str, str] = {}
    for d in topo.devices.values():
        if not d.on:
            issues.append(f"{d.name}: powered off")
        for p in d.ports.values():
            if p.iface:
                key = str(p.iface.ip)
                if key in seen_ip:
                    issues.append(f"duplicate IP {key}: {seen_ip[key]} and {d.name}:{p.name}")
                else:
                    seen_ip[key] = f"{d.name}:{p.name}"
                if p.iface.ip == p.iface.network.network_address and p.iface.network.prefixlen < 31:
                    issues.append(f"{d.name}:{p.name} uses the network address {p.ip}")
                if p.iface.network.prefixlen < 31 and p.iface.ip == p.iface.network.broadcast_address:
                    issues.append(f"{d.name}:{p.name} uses the broadcast address {p.ip}")
            if p.peer_dev and topo.is_l3(d.name, p.name) and p.up == 0:
                issues.append(f"{d.name}:{p.name} is cabled but administratively/physically down (no shutdown?)")
            elif p.peer_dev and d.has_cli and p.up == 1:
                issues.append(f"{d.name}:{p.name} line protocol down (encapsulation/clock rate/peer down?)")
    for seg in topo.segments():
        eps = seg["endpoints"]
        ifs = [(d, p, topo.port(d, p).iface) for d, p in eps]
        nets = {i.network for _, _, i in ifs if i}
        via = f" (via {', '.join(seg['switches'])})" if seg["switches"] else ""
        if len(nets) > 1:
            desc = ", ".join(f"{d}:{p}={i}" for d, p, i in ifs if i)
            issues.append(f"mixed subnets on one segment{via}: {desc}")
        routers = [(d, p, i) for d, p, i in ifs if topo.is_l3(d, p)]
        for d, p, i in ifs:
            dev = topo.devices[d]
            if not dev.is_host:
                continue
            pt = topo.port(d, p)
            if not i:
                if pt.extra.get("dhcp") or dev.dhcp:
                    issues.append(f"{d}: DHCP client without a lease (no DHCP server/relay reachable{via}?)")
                else:
                    issues.append(f"{d}: no IP address")
                continue
            if routers and dev.gw:
                rips = {str(ri.ip) for _, _, ri in routers if ri}
                rips |= {v for rd, rp, _ in routers for v in topo._cfg(rd, rp).get("vips", [])}
                if dev.gw not in rips:
                    try:
                        in_net = ipaddress.ip_address(dev.gw) in i.network
                    except ValueError:
                        in_net = False
                    issues.append(f"{d}: default gateway {dev.gw} is not a router on its segment "
                                  f"(routers: {', '.join(sorted(rips)) or 'none'}){'' if in_net else '; not even in ' + str(i.network)}")
            elif dev.gw and not routers:
                issues.append(f"{d}: default gateway {dev.gw} unreachable - no router/SVI on its segment{via} "
                              "(access VLAN, trunk allowed list or missing SVI?)")
            elif routers and not dev.gw and not (pt.extra.get("dhcp") or dev.dhcp):
                issues.append(f"{d}: no default gateway (router on segment: {routers[0][2] or routers[0][0]})")
    issues += unbound_acls(topo)
    linked = {(a, ap) for a, ap, *_ in topo.links} | {(b, bp) for _, _, b, bp, *_ in topo.links}
    for d in topo.devices.values():
        if d.is_host and not any((d.name, p) in linked for p in d.ports):
            issues.append(f"{d.name}: not cabled")
    return issues


_ACL_DEF = re.compile(r"^(?:ip access-list (?:standard|extended)|ipv6 access-list|access-list) (\S+)", re.M)
_ACL_USE = re.compile(r"(?:ip access-group|ipv6 traffic-filter|access-class|ipv6 access-class|ip nat inside source list"
                      r"|ip nat outside source list|match ip address|distribute-list) (\S+)")


def unbound_acls(topo: Topology) -> list[str]:
    """ACLs that are defined but referenced nowhere (an access-group that was never applied, or lost)."""
    out = []
    for name, cfg in topo.configs.items():
        defined = list(dict.fromkeys(_ACL_DEF.findall(cfg)))
        used = set(_ACL_USE.findall(cfg))
        for acl in defined:
            if acl in used:
                continue
            d = topo.devices.get(name)
            if d and d.type in L3_SWITCH_TYPES:
                msg = (f"{name}: ACL {acl} is defined but not applied anywhere in the config. PT's 3560/3650 never"
                       " write an SVI's 'ip access-group' to the config, so it may be filtering now but is lost"
                       " whenever the .pkt is reopened: re-apply it after opening the file")
            else:
                msg = f"{name}: ACL {acl} is defined but not applied anywhere (missing ip access-group?)"
            out.append(msg)
    return out
