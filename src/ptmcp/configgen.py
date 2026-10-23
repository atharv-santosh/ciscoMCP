"""IOS config generators. Each one returns a list of global config lines."""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Callable

from .netcalc import wildcard


def _iface(cidr: str) -> tuple[str, str]:
    i = ipaddress.ip_interface(cidr)
    return str(i.ip), str(i.network.netmask)


def _net(cidr: str) -> ipaddress.IPv4Network:
    return ipaddress.ip_network(cidr, strict=False)


_PORT_RE = re.compile(r"^(.*?)(\d+)$")


def _expand(ports: str | list[str]) -> list[str]:
    """Interface list from 'Fa0/1 - 4, Gi0/1' or a list. No 'interface range': PT's CLI breaks on it."""
    items = ports if isinstance(ports, list) else [p for p in ports.split(",")]
    out: list[str] = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            left, right = (x.strip() for x in item.split("-", 1))
            m = _PORT_RE.match(left)
            if m and right.isdigit():
                out += [f"{m.group(1)}{n}" for n in range(int(m.group(2)), int(right) + 1)]
                continue
        out.append(item)
    return out


def _headers(ports: str | list[str]) -> list[str]:
    return [f"interface {p}" for p in _expand(ports)]


def _each(ports, body: list[str]) -> list[str]:
    c = []
    for h in _headers(ports):
        c += [h] + body
    return c


def basic(hostname: str, enable_secret: str | None = "class", console_password: str | None = "cisco",
          vty_password: str | None = "cisco", banner: str | None = "Authorized access only",
          domain: str | None = None, encrypt: bool = True, no_lookup: bool = True,
          exec_timeout: str | None = None) -> list[str]:
    c = [f"hostname {hostname}"]
    if no_lookup:
        c.append("no ip domain-lookup")
    if domain:
        c.append(f"ip domain-name {domain}")
    if enable_secret:
        c.append(f"enable secret {enable_secret}")
    if encrypt:
        c.append("service password-encryption")
    if banner:
        c.append(f"banner motd #{banner}#")
    if console_password:
        c += ["line console 0", f" password {console_password}", " login", " logging synchronous"]
        if exec_timeout:
            c.append(f" exec-timeout {exec_timeout}")
        c.append(" exit")
    if vty_password:
        c += ["line vty 0 15", f" password {vty_password}", " login", " exit"]
    return c


def ssh(domain: str = "lab.local", username: str = "admin", password: str = "cisco123",
        modulus: int = 1024, version2: bool = True, local_auth: bool = True,
        hostname: str | None = None) -> list[str]:
    c = []
    if hostname:
        c.append(f"hostname {hostname}")
    c += [f"ip domain-name {domain}", f"username {username} privilege 15 secret {password}",
          f"crypto key generate rsa general-keys modulus {modulus}"]
    if version2:
        c.append("ip ssh version 2")
    c += ["line vty 0 15", " transport input ssh", " login local" if local_auth else " login", " exit"]
    return c


def interface(name: str, ip: str | None = None, description: str | None = None, shutdown: bool = False,
              ipv6: str | None = None, dhcp: bool = False, clock_rate: int | None = None,
              bandwidth: int | None = None, helper: str | None = None, nat: str | None = None,
              acl_in: str | None = None, acl_out: str | None = None) -> list[str]:
    c = [f"interface {name}"]
    if description:
        c.append(f" description {description}")
    if dhcp:
        c.append(" ip address dhcp")
    elif ip:
        a, m = _iface(ip)
        c.append(f" ip address {a} {m}")
    if ipv6:
        c.append(f" ipv6 address {ipv6}")
        c.append(" ipv6 enable")
    if clock_rate:
        c.append(f" clock rate {clock_rate}")
    if bandwidth:
        c.append(f" bandwidth {bandwidth}")
    if helper:
        c.append(f" ip helper-address {helper}")
    if nat in ("inside", "outside"):
        c.append(f" ip nat {nat}")
    if acl_in:
        c.append(f" ip access-group {acl_in} in")
    if acl_out:
        c.append(f" ip access-group {acl_out} out")
    c.append(" shutdown" if shutdown else " no shutdown")
    c.append(" exit")
    return c


def loopback(number: int, ip: str, description: str | None = None) -> list[str]:
    return interface(f"Loopback{number}", ip, description)


def vlans(vlans: dict[int | str, str] | list[int]) -> list[str]:
    items = vlans.items() if isinstance(vlans, dict) else [(v, None) for v in vlans]
    c = []
    for vid, name in items:
        c.append(f"vlan {vid}")
        if name:
            c.append(f" name {name}")
        c.append(" exit")
    return c


def access_ports(ports: str | list[str], vlan: int, voice_vlan: int | None = None, portfast: bool = True,
                 bpduguard: bool = False, description: str | None = None) -> list[str]:
    body = [f" description {description}"] if description else []
    body += [" switchport mode access", f" switchport access vlan {vlan}"]
    if voice_vlan:
        body.append(f" switchport voice vlan {voice_vlan}")
    if portfast:
        body.append(" spanning-tree portfast")
    if bpduguard:
        body.append(" spanning-tree bpduguard enable")
    return _each(ports, body + [" no shutdown", " exit"])


def trunk(ports: str | list[str], allowed: str | list[int] | None = None, native: int | None = None,
          dot1q_encap: bool = False, nonegotiate: bool = False, description: str | None = None) -> list[str]:
    """dot1q_encap=True for 3560/3650 (they also support ISL, so encapsulation must be set first)."""
    body = [f" description {description}"] if description else []
    if dot1q_encap:
        body.append(" switchport trunk encapsulation dot1q")
    body.append(" switchport mode trunk")
    if native:
        body.append(f" switchport trunk native vlan {native}")
    if allowed:
        al = allowed if isinstance(allowed, str) else ",".join(str(v) for v in allowed)
        body.append(f" switchport trunk allowed vlan {al}")
    if nonegotiate:
        body.append(" switchport nonegotiate")
    return _each(ports, body + [" no shutdown", " exit"])


def shutdown_unused(ports: str | list[str], parking_vlan: int | None = 999) -> list[str]:
    body = [" switchport mode access", f" switchport access vlan {parking_vlan}"] if parking_vlan else []
    return _each(ports, body + [" shutdown", " exit"])


def port_security(ports: str | list[str], maximum: int = 1, violation: str = "shutdown",
                  sticky: bool = True, mac: str | None = None) -> list[str]:
    body = [" switchport mode access", " switchport port-security", f" switchport port-security maximum {maximum}",
            f" switchport port-security violation {violation}"]
    if sticky:
        body.append(" switchport port-security mac-address sticky")
    if mac:
        body.append(f" switchport port-security mac-address {mac}")
    return _each(ports, body + [" exit"])


def stp(mode: str = "rapid-pvst", root_primary: list[int] | None = None, root_secondary: list[int] | None = None,
        priority: dict[int, int] | None = None, portfast_default: bool = False,
        bpduguard_default: bool = False) -> list[str]:
    c = [f"spanning-tree mode {mode}"]
    if root_primary:
        c.append(f"spanning-tree vlan {','.join(map(str, root_primary))} root primary")
    if root_secondary:
        c.append(f"spanning-tree vlan {','.join(map(str, root_secondary))} root secondary")
    for vlan, prio in (priority or {}).items():
        c.append(f"spanning-tree vlan {vlan} priority {prio}")
    if portfast_default:
        c.append("spanning-tree portfast default")
    if bpduguard_default:
        c.append("spanning-tree portfast bpduguard default")
    return c


def etherchannel(ports: str | list[str], group: int = 1, mode: str = "active", trunk_mode: bool = True,
                 dot1q_encap: bool = False, native: int | None = None, allowed: str | None = None,
                 ip: str | None = None) -> list[str]:
    """mode: active/passive (LACP), desirable/auto (PAgP), on (static). ip -> routed L3 port-channel."""
    body = [" no switchport"] if ip else []
    if dot1q_encap and not ip:
        body.append(" switchport trunk encapsulation dot1q")
    if trunk_mode and not ip:
        body.append(" switchport mode trunk")
    c = _each(ports, body + [f" channel-group {group} mode {mode}", " no shutdown", " exit"])
    c.append(f"interface port-channel {group}")
    if ip:
        a, m = _iface(ip)
        c += [" no switchport", f" ip address {a} {m}"]
    elif trunk_mode:
        if dot1q_encap:
            c.append(" switchport trunk encapsulation dot1q")
        c.append(" switchport mode trunk")
        if native:
            c.append(f" switchport trunk native vlan {native}")
        if allowed:
            c.append(f" switchport trunk allowed vlan {allowed}")
    c.append(" exit")
    return c


def switch_mgmt(vlan: int, ip: str, gateway: str | None = None) -> list[str]:
    a, m = _iface(ip)
    c = [f"interface vlan {vlan}", f" ip address {a} {m}", " no shutdown", " exit"]
    if gateway:
        c.append(f"ip default-gateway {gateway}")
    return c


def router_on_a_stick(parent: str, vlans: list[dict], native: int | None = None) -> list[str]:
    """vlans: [{id, ip:'192.168.10.1/24', name?, helper?}]"""
    c = [f"interface {parent}", " no ip address", " no shutdown", " exit"]
    for v in vlans:
        vid = int(v["id"])
        a, m = _iface(v["ip"])
        c.append(f"interface {parent}.{vid}")
        if v.get("name"):
            c.append(f" description {v['name']}")
        c.append(f" encapsulation dot1Q {vid}{' native' if native == vid else ''}")
        c.append(f" ip address {a} {m}")
        if v.get("helper"):
            c.append(f" ip helper-address {v['helper']}")
        c.append(" exit")
    return c


def svi(vlans: list[dict], ip_routing: bool = True) -> list[str]:
    """Layer-3 switch SVIs. vlans: [{id, ip, name?, helper?}]"""
    c = ["ip routing"] if ip_routing else []
    for v in vlans:
        a, m = _iface(v["ip"])
        c += [f"vlan {v['id']}"] + ([f" name {v['name']}"] if v.get("name") else []) + [" exit"]
        c += [f"interface vlan {v['id']}", f" ip address {a} {m}"]
        if v.get("helper"):
            c.append(f" ip helper-address {v['helper']}")
        c += [" no shutdown", " exit"]
    return c


def routed_port(port: str, ip: str, description: str | None = None) -> list[str]:
    a, m = _iface(ip)
    c = [f"interface {port}", " no switchport", f" ip address {a} {m}"]
    if description:
        c.append(f" description {description}")
    return c + [" no shutdown", " exit"]


def ospf(process: int = 1, networks: list[str] | list[dict] | None = None, area: int | str = 0,
         router_id: str | None = None, passive: list[str] | None = None, passive_default: bool = False,
         active: list[str] | None = None, default_originate: bool = False,
         reference_bandwidth: int | None = None, interfaces: list[dict] | None = None) -> list[str]:
    """networks: ['10.0.0.0/30', ...] (uses `area`) or [{net, area}].
    interfaces: [{name, area, cost?, priority?, p2p?}] for interface-mode config."""
    c = [f"router ospf {process}"]
    if router_id:
        c.append(f" router-id {router_id}")
    if reference_bandwidth:
        c.append(f" auto-cost reference-bandwidth {reference_bandwidth}")
    for n in networks or []:
        net, ar = (n["net"], n.get("area", area)) if isinstance(n, dict) else (n, area)
        nn = _net(net)
        c.append(f" network {nn.network_address} {wildcard(nn.prefixlen)} area {ar}")
    if passive_default:
        c.append(" passive-interface default")
        for i in active or []:
            c.append(f" no passive-interface {i}")
    for i in passive or []:
        c.append(f" passive-interface {i}")
    if default_originate:
        c.append(" default-information originate")
    c.append(" exit")
    for i in interfaces or []:
        c.append(f"interface {i['name']}")
        c.append(f" ip ospf {process} area {i.get('area', area)}")
        if i.get("cost"):
            c.append(f" ip ospf cost {i['cost']}")
        if i.get("priority") is not None:
            c.append(f" ip ospf priority {i['priority']}")
        if i.get("p2p"):
            c.append(" ip ospf network point-to-point")
        c.append(" exit")
    return c


def eigrp(asn: int = 100, networks: list[str] | None = None, router_id: str | None = None,
          passive: list[str] | None = None, no_auto_summary: bool = True, wildcards: bool = True,
          redistribute_static: bool = False) -> list[str]:
    c = [f"router eigrp {asn}"]
    if router_id:
        c.append(f" eigrp router-id {router_id}")
    for n in networks or []:
        nn = _net(n)
        c.append(f" network {nn.network_address} {wildcard(nn.prefixlen)}" if wildcards else f" network {nn.network_address}")
    if no_auto_summary:
        c.append(" no auto-summary")
    for i in passive or []:
        c.append(f" passive-interface {i}")
    if redistribute_static:
        c.append(" redistribute static")
    c.append(" exit")
    return c


def rip(networks: list[str] | None = None, version: int = 2, no_auto_summary: bool = True,
        passive: list[str] | None = None, default_originate: bool = False) -> list[str]:
    c = ["router rip", f" version {version}"]
    seen = set()
    for n in networks or []:
        nn = _net(n)
        classful = _classful(nn)
        if classful not in seen:
            seen.add(classful)
            c.append(f" network {classful}")
    if no_auto_summary:
        c.append(" no auto-summary")
    for i in passive or []:
        c.append(f" passive-interface {i}")
    if default_originate:
        c.append(" default-information originate")
    c.append(" exit")
    return c


def _classful(n: ipaddress.IPv4Network) -> str:
    o = int(str(n.network_address).split(".")[0])
    plen = 8 if o < 128 else 16 if o < 192 else 24
    return str(ipaddress.ip_network(f"{n.network_address}/{plen}", strict=False).network_address)


def static_routes(routes: list[dict]) -> list[str]:
    """routes: [{dest:'10.1.0.0/16', via:'10.0.0.2' | exit interface, ad?}] ; dest '0.0.0.0/0' = default."""
    c = []
    for r in routes:
        n = _net(r["dest"])
        line = f"ip route {n.network_address} {n.netmask} {r['via']}"
        if r.get("ad"):
            line += f" {r['ad']}"
        c.append(line)
    return c


def bgp(asn: int, neighbors: list[dict], networks: list[str] | None = None, router_id: str | None = None) -> list[str]:
    c = [f"router bgp {asn}"]
    if router_id:
        c.append(f" bgp router-id {router_id}")
    for nb in neighbors:
        c.append(f" neighbor {nb['ip']} remote-as {nb['asn']}")
    for n in networks or []:
        nn = _net(n)
        c.append(f" network {nn.network_address} mask {nn.netmask}")
    c.append(" exit")
    return c


def dhcp_pool(name: str, network: str, gateway: str | None = None, dns: str | None = None,
              domain: str | None = None, exclude: list[str] | None = None, exclude_first: int = 0,
              lease_days: int | None = None) -> list[str]:
    """exclude: ['192.168.1.1-192.168.1.10', '192.168.1.254']; exclude_first reserves the first N hosts."""
    n = _net(network)
    c = []
    if exclude_first:
        c.append(f"ip dhcp excluded-address {n[1]} {n[exclude_first]}")
    for e in exclude or []:
        lo, _, hi = e.partition("-")
        c.append(f"ip dhcp excluded-address {lo.strip()}" + (f" {hi.strip()}" if hi else ""))
    c += [f"ip dhcp pool {name}", f" network {n.network_address} {n.netmask}"]
    c.append(f" default-router {gateway or n[1]}")
    if dns:
        c.append(f" dns-server {dns}")
    if domain:
        c.append(f" domain-name {domain}")
    if lease_days:
        c.append(f" lease {lease_days}")
    c.append(" exit")
    return c


def dhcp_relay(interface_name: str, server: str) -> list[str]:
    return [f"interface {interface_name}", f" ip helper-address {server}", " exit"]


def nat_overload(inside: list[str], outside: str, networks: list[str], acl: int | str = 1,
                 pool: dict | None = None) -> list[str]:
    """PAT. pool={name, start, end, prefix} uses a public pool; otherwise overload the outside interface."""
    c = []
    for n in networks:
        nn = _net(n)
        c.append(f"access-list {acl} permit {nn.network_address} {wildcard(nn.prefixlen)}")
    if pool:
        c.append(f"ip nat pool {pool['name']} {pool['start']} {pool['end']} netmask "
                 f"{ipaddress.ip_network('0.0.0.0/' + str(pool.get('prefix', 29))).netmask}")
        c.append(f"ip nat inside source list {acl} pool {pool['name']} overload")
    else:
        c.append(f"ip nat inside source list {acl} interface {outside} overload")
    for i in inside:
        c += [f"interface {i}", " ip nat inside", " exit"]
    c += [f"interface {outside}", " ip nat outside", " exit"]
    return c


def nat_static(inside_local: str, inside_global: str, inside: list[str] | None = None,
               outside: str | None = None) -> list[str]:
    c = [f"ip nat inside source static {inside_local} {inside_global}"]
    for i in inside or []:
        c += [f"interface {i}", " ip nat inside", " exit"]
    if outside:
        c += [f"interface {outside}", " ip nat outside", " exit"]
    return c


def _acl_addr(spec: str) -> str:
    spec = spec.strip()
    if spec in ("any", "*"):
        return "any"
    if spec.startswith("host "):
        return spec
    if "/" in spec:
        n = _net(spec)
        if n.prefixlen == 32:
            return f"host {n.network_address}"
        return f"{n.network_address} {wildcard(n.prefixlen)}"
    return f"host {spec}"


def acl(name: str | int, rules: list[dict | str], extended: bool | None = None,
        apply: list[dict] | None = None) -> list[str]:
    """Named or numbered ACL.
    rules: [{action:'permit', proto:'tcp', src:'10.0.0.0/24', dst:'any', port:80, op:'eq'}] or raw strings.
    apply: [{interface, dir:'in'|'out'}] or [{vty:true}]"""
    is_num = str(name).isdigit()
    if extended is None:
        extended = (int(name) >= 100) if is_num else any(isinstance(r, dict) and (r.get("proto") or r.get("dst")) for r in rules)
    lines = []
    for r in rules:
        if isinstance(r, str):
            lines.append(r.strip())
            continue
        act = r.get("action", "permit")
        if not extended:
            lines.append(f"{act} {_acl_addr(r.get('src', 'any'))}")
            continue
        proto = r.get("proto", "ip")
        ln = f"{act} {proto} {_acl_addr(r.get('src', 'any'))} {_acl_addr(r.get('dst', 'any'))}"
        if r.get("port") is not None:
            ln += f" {r.get('op', 'eq')} {r['port']}"
        if r.get("established"):
            ln += " established"
        if r.get("icmp_type"):
            ln += f" {r['icmp_type']}"
        lines.append(ln)
    if is_num:
        c = [f"access-list {name} {ln}" for ln in lines]
    else:
        c = [f"ip access-list {'extended' if extended else 'standard'} {name}"] + [f" {ln}" for ln in lines] + [" exit"]
    for a in apply or []:
        if a.get("vty"):
            c += ["line vty 0 15", f" access-class {name} in", " exit"]
        else:
            c += [f"interface {a['interface']}", f" ip access-group {name} {a.get('dir', 'in')}", " exit"]
    return c


def hsrp(interface_name: str, group: int, vip: str, priority: int | None = None, preempt: bool = True,
         version2: bool = False) -> list[str]:
    c = [f"interface {interface_name}"]
    if version2:
        c.append(" standby version 2")
    c.append(f" standby {group} ip {vip}")
    if priority:
        c.append(f" standby {group} priority {priority}")
    if preempt:
        c.append(f" standby {group} preempt")
    c.append(" exit")
    return c


def ipv6_routing(ospfv3_pid: int | None = None, router_id: str | None = None,
                 interfaces: list[dict] | None = None) -> list[str]:
    """interfaces: [{name, ipv6:'2001:db8:1::1/64', area?}]"""
    c = ["ipv6 unicast-routing"]
    if ospfv3_pid:
        c += [f"ipv6 router ospf {ospfv3_pid}"] + ([f" router-id {router_id}"] if router_id else []) + [" exit"]
    for i in interfaces or []:
        c += [f"interface {i['name']}", f" ipv6 address {i['ipv6']}", " ipv6 enable"]
        if ospfv3_pid:
            c.append(f" ipv6 ospf {ospfv3_pid} area {i.get('area', 0)}")
        c += [" no shutdown", " exit"]
    return c


def ntp_syslog(ntp_server: str | None = None, syslog_server: str | None = None, timestamps: bool = True) -> list[str]:
    c = []
    if ntp_server:
        c.append(f"ntp server {ntp_server}")
    if syslog_server:
        c += [f"logging host {syslog_server}", "logging trap debugging"]
    if timestamps:
        c.append("service timestamps log datetime msec")
    return c


def harden(hostname: str | None = None, min_password_length: int = 10, login_block: bool = True,
           disable_http: bool = False, disable_cdp: bool = False) -> list[str]:
    c = [] if not hostname else [f"hostname {hostname}"]
    c += ["service password-encryption", f"security passwords min-length {min_password_length}"]
    if login_block:
        c.append("login block-for 120 attempts 3 within 60")
    if disable_http:
        c.append("no ip http server")  # not accepted by every PT IOS image
    if disable_cdp:
        c.append("no cdp run")
    c += ["line console 0", " exec-timeout 5 0", " exit", "line vty 0 15", " exec-timeout 5 0", " exit"]
    return c


FEATURES: dict[str, Callable[..., list[str]]] = {
    "basic": basic, "ssh": ssh, "interface": interface, "loopback": loopback,
    "vlans": vlans, "access_ports": access_ports, "trunk": trunk, "shutdown_unused": shutdown_unused,
    "port_security": port_security, "stp": stp, "etherchannel": etherchannel, "switch_mgmt": switch_mgmt,
    "router_on_a_stick": router_on_a_stick, "svi": svi, "routed_port": routed_port,
    "ospf": ospf, "eigrp": eigrp, "rip": rip, "static_routes": static_routes, "bgp": bgp,
    "dhcp_pool": dhcp_pool, "dhcp_relay": dhcp_relay, "nat_overload": nat_overload, "nat_static": nat_static,
    "acl": acl, "hsrp": hsrp, "ipv6_routing": ipv6_routing, "ntp_syslog": ntp_syslog, "harden": harden,
}


def feature_help() -> str:
    import inspect
    out = []
    for name, fn in FEATURES.items():
        sig = str(inspect.signature(fn)).replace("NoneType", "None")
        doc = (fn.__doc__ or "").strip().splitlines()
        out.append(f"{name}{sig}" + (f"\n    {' '.join(d.strip() for d in doc)}" if doc else ""))
    return "\n".join(out)


def generate(feature: str, params: dict[str, Any]) -> list[str]:
    if feature not in FEATURES:
        raise ValueError(f"unknown feature {feature!r}; one of: {', '.join(FEATURES)}")
    return FEATURES[feature](**params)
