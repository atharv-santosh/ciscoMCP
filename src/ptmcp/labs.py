"""Lab blueprints."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any, Callable

from . import configgen as cg

SERIAL_SLOTS = ["0/0", "0/1", "0/2", "0/3", "0", "1", "2"]


@dataclass
class LabSpec:
    title: str
    devices: list[dict] = field(default_factory=list)          # {name, model, x, y, modules?:[{slot|slots, module}]}
    links: list[dict] = field(default_factory=list)            # {a, b, ap?, bp?, cable?, prefA?, prefB?}
    cmds: dict[str, list[str]] = field(default_factory=dict)   # device -> config lines
    hosts: dict[str, dict] = field(default_factory=dict)       # host -> {ip, mask, gw, dns} | {dhcp: True}
    auto: dict[str, Any] | None = None                         # planner.auto_address kwargs
    post: Callable[[dict], dict[str, list[str]]] | None = None  # extra cmds computed from chosen ports
    checks: list[tuple[str, str]] = field(default_factory=list)  # ping pairs (src host, dst host/ip)
    notes: list[str] = field(default_factory=list)

    def add(self, name, model, x, y, modules=None):
        d = {"name": name, "model": model, "x": x, "y": y}
        if modules:
            d["modules"] = modules
        self.devices.append(d)

    def link(self, a, b, ap=None, bp=None, cable="auto", **kw):
        self.links.append({"a": a, "b": b, "ap": ap, "bp": bp, "cable": cable, **kw})

    def cfg(self, dev, lines):
        self.cmds.setdefault(dev, []).extend(lines)


def _mask(net):
    return str(ipaddress.ip_network(net).netmask)


def _host(net, i):
    return str(ipaddress.ip_network(net)[i])


def small_office(pcs: int = 3, network: str = "192.168.1.0/24", dhcp: bool = True, secure: bool = True,
                 server: bool = False) -> LabSpec:
    """One router, one switch, N PCs (+ optional server). DHCP from the router, SSH-hardened."""
    L = LabSpec("Small office LAN")
    L.add("R1", "2911", 400, 100)
    L.add("S1", "2960-24TT", 400, 250)
    L.link("R1", "S1", "GigabitEthernet0/0", "GigabitEthernet0/1")
    gw = _host(network, 1)
    L.cfg("R1", cg.basic("R1") + cg.interface("GigabitEthernet0/0", f"{gw}/{ipaddress.ip_network(network).prefixlen}", "LAN"))
    if dhcp:
        L.cfg("R1", cg.dhcp_pool("LAN", network, gw, dns=_host(network, 5) if server else None, exclude_first=10))
    if secure:
        L.cfg("R1", cg.ssh("office.local", "admin", "Cisco123!") + cg.harden(min_password_length=8))
    L.cfg("S1", cg.basic("S1") + cg.switch_mgmt(1, f"{_host(network, 2)}/{ipaddress.ip_network(network).prefixlen}", gw))
    for i in range(pcs):
        name = f"PC{i + 1}"
        L.add(name, "PC-PT", 160 + i * 140, 420)
        L.link("S1", name, f"FastEthernet0/{i + 1}", "FastEthernet0")
        L.hosts[name] = {"dhcp": True} if dhcp else {"ip": _host(network, 11 + i), "mask": _mask(network), "gw": gw}
        L.checks.append((name, gw))
    if server:
        L.add("SRV1", "Server-PT", 650, 420)
        L.link("S1", "SRV1", "FastEthernet0/24", "FastEthernet0")
        L.hosts["SRV1"] = {"ip": _host(network, 5), "mask": _mask(network), "gw": gw}
        L.checks.append(("PC1", _host(network, 5)))
    if pcs > 1:
        L.checks.append(("PC1", f"PC{pcs}"))
    L.notes.append("DHCP leases take a few seconds; pt_ping waits for them." if dhcp else "Static addressing .11+")
    return L


def router_on_a_stick(vlans: list[int] | None = None, pcs_per_vlan: int = 2, base: str = "192.168.0.0/16",
                      dhcp: bool = True) -> LabSpec:
    """Inter-VLAN routing with 802.1Q subinterfaces on one router and a trunk to a 2960."""
    vlans = vlans or [10, 20, 30]
    L = LabSpec("Router-on-a-stick inter-VLAN routing")
    L.add("R1", "2911", 450, 80)
    L.add("S1", "2960-24TT", 450, 230)
    L.link("R1", "S1", "GigabitEthernet0/0", "GigabitEthernet0/1")
    names = {v: f"VLAN{v}" for v in vlans}
    sub, access = [], []
    port = 1
    octets = str(ipaddress.ip_network(base).network_address).split(".")
    for idx, v in enumerate(vlans):
        net = f"{octets[0]}.{octets[1]}.{v}.0/24"
        gw = _host(net, 1)
        sub.append({"id": v, "ip": f"{gw}/24", "name": names[v]})
        if dhcp:
            L.cfg("R1", cg.dhcp_pool(names[v], net, gw, exclude_first=10))
        ports = []
        for j in range(pcs_per_vlan):
            name = f"PC{v}-{j + 1}"
            L.add(name, "PC-PT", 120 + (idx * pcs_per_vlan + j) * 120, 420)
            L.link("S1", name, f"FastEthernet0/{port}", "FastEthernet0")
            ports.append(f"FastEthernet0/{port}")
            port += 1
            L.hosts[name] = {"dhcp": True} if dhcp else {"ip": _host(net, 11 + j), "mask": "255.255.255.0", "gw": gw}
        access += cg.access_ports(ports, v)
    L.cfg("R1", ["hostname R1"] + cg.router_on_a_stick("GigabitEthernet0/0", sub))
    L.cfg("S1", ["hostname S1"] + cg.vlans(names) + cg.trunk("GigabitEthernet0/1", allowed=vlans) + access)
    first = [f"PC{v}-1" for v in vlans]
    L.checks = [(first[0], f) for f in first[1:]]
    if pcs_per_vlan > 1:
        L.checks.append((first[0], f"PC{vlans[0]}-2"))
    return L


def intervlan_l3(vlans: list[int] | None = None, access_switches: int = 2, pcs_per_vlan: int = 1,
                 dhcp: bool = True) -> LabSpec:
    """3560 distribution switch with SVIs + ip routing, 2960 access switches on dot1q trunks."""
    vlans = vlans or [10, 20]
    L = LabSpec("Inter-VLAN routing on a Layer-3 switch")
    L.add("DSW1", "3560-24PS", 450, 80)
    names = {v: f"VLAN{v}" for v in vlans}
    svis = []
    for v in vlans:
        net = f"10.{v}.0.0/24"
        svis.append({"id": v, "ip": f"10.{v}.0.1/24", "name": names[v]})
        if dhcp:
            L.cfg("DSW1", cg.dhcp_pool(names[v], net, f"10.{v}.0.1", exclude_first=10))
    L.cfg("DSW1", ["hostname DSW1"] + cg.svi(svis) + cg.stp("rapid-pvst", root_primary=vlans))
    pcn = 0
    for a in range(access_switches):
        sw = f"ASW{a + 1}"
        x = 200 + a * 500 / max(access_switches - 1, 1) if access_switches > 1 else 450
        L.add(sw, "2960-24TT", int(x), 230)
        up = f"GigabitEthernet0/{a + 1}"
        L.link("DSW1", sw, up, "GigabitEthernet0/1")
        L.cfg("DSW1", cg.trunk(up, allowed=vlans, dot1q_encap=True))
        L.cfg(sw, [f"hostname {sw}"] + cg.vlans(names) + cg.trunk("GigabitEthernet0/1", allowed=vlans) + cg.stp("rapid-pvst"))
        port = 1
        for v in vlans:
            for j in range(pcs_per_vlan):
                pcn += 1
                name = f"PC{pcn}"
                L.add(name, "PC-PT", int(x) - 120 + (port - 1) * 90, 400)
                L.link(sw, name, f"FastEthernet0/{port}", "FastEthernet0")
                L.cfg(sw, cg.access_ports(f"FastEthernet0/{port}", v))
                L.hosts[name] = {"dhcp": True} if dhcp else {"ip": f"10.{v}.0.{10 + pcn}", "mask": "255.255.255.0", "gw": f"10.{v}.0.1"}
                port += 1
    pcs = [d["name"] for d in L.devices if d["name"].startswith("PC")]
    L.checks = [(pcs[0], p) for p in pcs[1:]]
    return L


def routing(protocol: str = "ospf", routers: int = 3, shape: str = "ring", serial: bool = False,
            lan_pcs: int = 1, model: str = "2911") -> LabSpec:
    """N routers (chain|ring|star|mesh), each with a LAN switch + PCs, addressed and routed automatically.
    protocol: ospf | eigrp | rip | static. serial=True uses HWIC-2T serial WAN links."""
    shape = shape.lower()
    L = LabSpec(f"{protocol.upper()} {shape} of {routers} routers" + (" over serial" if serial else ""))
    import math
    names = [f"R{i + 1}" for i in range(routers)]
    pairs: list[tuple[str, str]] = []
    if shape == "chain":
        pairs = list(zip(names, names[1:]))
    elif shape == "ring":
        pairs = list(zip(names, names[1:])) + ([(names[-1], names[0])] if routers > 2 else [])
    elif shape == "star":
        pairs = [(names[0], n) for n in names[1:]]
    elif shape == "mesh":
        pairs = [(a, b) for i, a in enumerate(names) for b in names[i + 1:]]
    else:
        raise ValueError("shape must be chain, ring, star or mesh")
    degree = {n: sum(n in p for p in pairs) for n in names}
    eth_needed = max(degree.values()) + 1
    if not serial and model == "2911" and eth_needed > 3:
        model = "Router-PT"
        L.notes.append("Using Router-PT (+ PT-ROUTER-NM-1CFE modules): the 2911 has only 3 ethernet ports")
    cx, cy, r = 500, 260, 200
    for i, n in enumerate(names):
        if shape == "chain":
            x, y = 120 + i * (760 / max(routers - 1, 1)), 200
        elif shape == "star" and i == 0:
            x, y = cx, cy
        else:
            k = i - (1 if shape == "star" else 0)
            tot = routers - (1 if shape == "star" else 0)
            ang = -math.pi / 2 + 2 * math.pi * k / max(tot, 1)
            x, y = cx + r * math.cos(ang), cy + r * 0.8 * math.sin(ang)
        mods = None
        if serial and model in ("1941", "2901", "2911"):
            need = math.ceil(degree[n] / 2)
            mods = [{"slots": SERIAL_SLOTS, "module": "HWIC-2T"} for _ in range(need)]
        elif not serial and model == "Router-PT" and degree[n] + 1 > 2:
            # Router-PT only has 2 copper FE ports
            mods = [{"slots": [str(s) for s in range(6, 10)], "module": "PT-ROUTER-NM-1CFE"}
                    for _ in range(degree[n] + 1 - 2)]
        L.add(n, model, int(x), int(y), mods)
        sw = f"S{i + 1}"
        L.add(sw, "2960-24TT", int(x), int(y) + 120 if shape != "chain" else int(y) + 130)
        L.link(n, sw, cable="auto", prefB="fast")
        for j in range(lan_pcs):
            pc = f"PC{i + 1}" if lan_pcs == 1 else f"PC{i + 1}-{j + 1}"
            L.add(pc, "PC-PT", int(x) - 40 * (lan_pcs - 1) + 80 * j, int(y) + 230)
            L.link(sw, pc, prefA="access")
    for a, b in pairs:
        L.link(a, b, cable="serial" if serial else "auto")
    L.auto = {"routing": protocol, "lan_pool": "192.168.0.0/16", "p2p_pool": "10.0.0.0/24", "dhcp": False}
    if serial:
        def clock(ports_by_link: dict) -> dict[str, list[str]]:
            extra: dict[str, list[str]] = {}
            for (a, ap, b, bp, cable) in ports_by_link["links"]:
                if cable == "serial":
                    # set on both ends, the DTE side rejects it
                    extra.setdefault(a, []).extend([f"interface {ap}", " clock rate 64000", " exit"])
                    extra.setdefault(b, []).extend([f"interface {bp}", " clock rate 64000", " exit"])
            return extra
        L.post = clock
        L.notes.append("clock rate is pushed to both serial ends; the DTE end's rejection is expected")
    pcs = [d["name"] for d in L.devices if d["name"].startswith("PC")]
    L.checks = [(pcs[0], p) for p in pcs[1:]]
    return L


def nat_internet(pcs: int = 2) -> LabSpec:
    """LAN -> edge router doing PAT + DHCP -> ISP router -> 'internet' web server. ISP has no route to the LAN."""
    L = LabSpec("Edge NAT/PAT to an ISP")
    L.add("EDGE", "2911", 350, 200)
    L.add("ISP", "2911", 650, 200)
    L.add("LAN-SW", "2960-24TT", 350, 330)
    L.add("WEB", "Server-PT", 900, 200)
    L.link("EDGE", "LAN-SW", "GigabitEthernet0/0", "GigabitEthernet0/1")
    L.link("EDGE", "ISP", "GigabitEthernet0/1", "GigabitEthernet0/1")
    L.link("ISP", "WEB", "GigabitEthernet0/0", "FastEthernet0")
    L.cfg("EDGE", ["hostname EDGE"]
          + cg.interface("GigabitEthernet0/0", "192.168.1.1/24", "LAN")
          + cg.interface("GigabitEthernet0/1", "203.0.113.2/30", "to ISP")
          + cg.dhcp_pool("LAN", "192.168.1.0/24", "192.168.1.1", dns="8.8.8.8", exclude_first=10)
          + cg.nat_overload(["GigabitEthernet0/0"], "GigabitEthernet0/1", ["192.168.1.0/24"])
          + cg.static_routes([{"dest": "0.0.0.0/0", "via": "203.0.113.1"}]))
    L.cfg("ISP", ["hostname ISP"]
          + cg.interface("GigabitEthernet0/1", "203.0.113.1/30", "customer EDGE")
          + cg.interface("GigabitEthernet0/0", "8.8.8.1/24", "internet"))
    L.hosts["WEB"] = {"ip": "8.8.8.8", "mask": "255.255.255.0", "gw": "8.8.8.1"}
    for i in range(pcs):
        n = f"PC{i + 1}"
        L.add(n, "PC-PT", 200 + i * 150, 470)
        L.link("LAN-SW", n, f"FastEthernet0/{i + 1}", "FastEthernet0")
        L.hosts[n] = {"dhcp": True}
        L.checks.append((n, "8.8.8.8"))
    L.notes.append("verify with: pt_show EDGE 'show ip nat translations' after pinging 8.8.8.8")
    return L


def hsrp_pair() -> LabSpec:
    """Two gateways sharing virtual IP 192.168.10.1 with HSRP; OSPF upstream to a core router + server."""
    L = LabSpec("HSRP first-hop redundancy")
    L.add("R1", "2911", 300, 180)
    L.add("R2", "2911", 600, 180)
    L.add("CORE", "2911", 450, 50)
    L.add("SW", "2960-24TT", 450, 320)
    L.add("SRV", "Server-PT", 750, 50)
    L.link("R1", "SW", "GigabitEthernet0/0", "GigabitEthernet0/1")
    L.link("R2", "SW", "GigabitEthernet0/0", "GigabitEthernet0/2")
    L.link("R1", "CORE", "GigabitEthernet0/1", "GigabitEthernet0/0")
    L.link("R2", "CORE", "GigabitEthernet0/1", "GigabitEthernet0/1")
    L.link("CORE", "SRV", "GigabitEthernet0/2", "FastEthernet0")
    L.cfg("R1", ["hostname R1"] + cg.interface("GigabitEthernet0/0", "192.168.10.2/24")
          + cg.hsrp("GigabitEthernet0/0", 10, "192.168.10.1", priority=110)
          + cg.interface("GigabitEthernet0/1", "10.0.0.2/30")
          + cg.ospf(1, ["192.168.10.0/24", "10.0.0.0/30"], router_id="1.1.1.1", passive=["GigabitEthernet0/0"]))
    L.cfg("R2", ["hostname R2"] + cg.interface("GigabitEthernet0/0", "192.168.10.3/24")
          + cg.hsrp("GigabitEthernet0/0", 10, "192.168.10.1", priority=100)
          + cg.interface("GigabitEthernet0/1", "10.0.0.6/30")
          + cg.ospf(1, ["192.168.10.0/24", "10.0.0.4/30"], router_id="2.2.2.2", passive=["GigabitEthernet0/0"]))
    L.cfg("CORE", ["hostname CORE"] + cg.interface("GigabitEthernet0/0", "10.0.0.1/30")
          + cg.interface("GigabitEthernet0/1", "10.0.0.5/30") + cg.interface("GigabitEthernet0/2", "172.16.0.1/24")
          + cg.ospf(1, ["10.0.0.0/30", "10.0.0.4/30", "172.16.0.0/24"], router_id="3.3.3.3", passive=["GigabitEthernet0/2"]))
    L.hosts["SRV"] = {"ip": "172.16.0.10", "mask": "255.255.255.0", "gw": "172.16.0.1"}
    for i in range(2):
        n = f"PC{i + 1}"
        L.add(n, "PC-PT", 350 + i * 200, 450)
        L.link("SW", n, f"FastEthernet0/{i + 1}", "FastEthernet0")
        L.hosts[n] = {"ip": f"192.168.10.{11 + i}", "mask": "255.255.255.0", "gw": "192.168.10.1"}
        L.checks.append((n, "172.16.0.10"))
    L.notes.append("test failover: pt_update_device R1 power=false, then pt_ping PC1 172.16.0.10")
    return L


def etherchannel_stp(vlans: list[int] | None = None) -> LabSpec:
    """Two 3560 distribution switches joined by a 2-link LACP trunk, an access 2960 dual-homed, Rapid-PVST root placement."""
    vlans = vlans or [10, 20]
    L = LabSpec("EtherChannel + Rapid-PVST")
    L.add("DSW1", "3560-24PS", 300, 80)
    L.add("DSW2", "3560-24PS", 650, 80)
    L.add("ASW1", "2960-24TT", 475, 260)
    L.link("DSW1", "DSW2", "FastEthernet0/23", "FastEthernet0/23")
    L.link("DSW1", "DSW2", "FastEthernet0/24", "FastEthernet0/24")
    L.link("DSW1", "ASW1", "GigabitEthernet0/1", "GigabitEthernet0/1")
    L.link("DSW2", "ASW1", "GigabitEthernet0/1", "GigabitEthernet0/2")
    names = {v: f"VLAN{v}" for v in vlans}
    for sw, prim, sec in (("DSW1", vlans, None), ("DSW2", None, vlans)):
        L.cfg(sw, [f"hostname {sw}"] + cg.vlans(names)
              + cg.etherchannel("FastEthernet0/23 - 24", 1, "active", dot1q_encap=True)
              + cg.trunk("GigabitEthernet0/1", dot1q_encap=True)
              + cg.stp("rapid-pvst", root_primary=prim, root_secondary=sec))
    L.cfg("ASW1", ["hostname ASW1"] + cg.vlans(names) + cg.trunk("GigabitEthernet0/1 - 2") + cg.stp("rapid-pvst"))
    for i, v in enumerate(vlans):
        n = f"PC{i + 1}"
        L.add(n, "PC-PT", 350 + i * 250, 420)
        L.link("ASW1", n, f"FastEthernet0/{i + 1}", "FastEthernet0")
        L.cfg("ASW1", cg.access_ports(f"FastEthernet0/{i + 1}", v))
        L.hosts[n] = {"ip": f"10.{v}.0.{11 + i}", "mask": "255.255.255.0", "gw": ""}
    if len(vlans) > 0:
        L.add("PC9", "PC-PT", 850, 420)
        L.link("ASW1", "PC9", "FastEthernet0/10", "FastEthernet0")
        L.cfg("ASW1", cg.access_ports("FastEthernet0/10", vlans[0]))
        L.hosts["PC9"] = {"ip": f"10.{vlans[0]}.0.99", "mask": "255.255.255.0", "gw": ""}
        L.checks.append(("PC1", "PC9"))
    L.notes.append("verify: pt_show DSW1 ['show etherchannel summary','show spanning-tree vlan %d']" % vlans[0])
    return L


def ipv6_dual_stack(routers: int = 2) -> LabSpec:
    """Dual-stack routers (IPv4 OSPFv2 + IPv6 OSPFv3), PCs with static IPv4 and SLAAC/static IPv6."""
    L = LabSpec("IPv6 dual stack with OSPFv3")
    names = [f"R{i + 1}" for i in range(routers)]
    for i, n in enumerate(names):
        x = 200 + i * (600 / max(routers - 1, 1))
        L.add(n, "2911", int(x), 150)
        L.add(f"S{i + 1}", "2960-24TT", int(x), 280)
        L.add(f"PC{i + 1}", "PC-PT", int(x), 410)
        L.link(n, f"S{i + 1}", "GigabitEthernet0/0", "GigabitEthernet0/1")
        L.link(f"S{i + 1}", f"PC{i + 1}", "FastEthernet0/1", "FastEthernet0")
        v4 = f"192.168.{i + 1}.0/24"
        L.hosts[f"PC{i + 1}"] = {"ip": f"192.168.{i + 1}.10", "mask": "255.255.255.0", "gw": f"192.168.{i + 1}.1",
                                 "ipv6": f"2001:db8:{i + 1}::10/64", "gw6": f"2001:db8:{i + 1}::1"}
        ifs6 = [{"name": "GigabitEthernet0/0", "ipv6": f"2001:db8:{i + 1}::1/64"}]
        nets4 = [v4]
        L.cfg(n, [f"hostname {n}"] + cg.interface("GigabitEthernet0/0", f"192.168.{i + 1}.1/24"))
        if i + 1 < routers:
            L.link(n, names[i + 1], "GigabitEthernet0/1", "GigabitEthernet0/2")
            L.cfg(n, cg.interface("GigabitEthernet0/1", f"10.0.{i}.1/30"))
            ifs6.append({"name": "GigabitEthernet0/1", "ipv6": f"2001:db8:ffff:{i}::1/64"})
            nets4.append(f"10.0.{i}.0/30")
        if i > 0:
            L.cfg(n, cg.interface("GigabitEthernet0/2", f"10.0.{i - 1}.2/30"))
            ifs6.append({"name": "GigabitEthernet0/2", "ipv6": f"2001:db8:ffff:{i - 1}::2/64"})
            nets4.append(f"10.0.{i - 1}.0/30")
        L.cfg(n, cg.ospf(1, nets4, router_id=f"{i + 1}.{i + 1}.{i + 1}.{i + 1}")
              + cg.ipv6_routing(1, f"{i + 1}.{i + 1}.{i + 1}.{i + 1}", ifs6))
    L.checks = [("PC1", f"PC{routers}"), ("PC1", f"2001:db8:{routers}::10")]
    return L


LABS: dict[str, Callable[..., LabSpec]] = {
    "small-office": small_office,
    "router-on-a-stick": router_on_a_stick,
    "intervlan-l3": intervlan_l3,
    "routing": routing,
    "nat-internet": nat_internet,
    "hsrp": hsrp_pair,
    "etherchannel-stp": etherchannel_stp,
    "ipv6-dual-stack": ipv6_dual_stack,
}


def lab_list() -> str:
    import inspect
    rows = []
    for k, fn in LABS.items():
        sig = str(inspect.signature(fn)).replace(" -> ptmcp.labs.LabSpec", "").replace(" -> LabSpec", "")
        rows.append(f"{k}{sig}\n    {(fn.__doc__ or '').strip()}")
    return "\n".join(rows)
