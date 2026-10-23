"""Subnetting helpers: subnet info, VLSM, summarization."""

from __future__ import annotations

import ipaddress
import math
from dataclasses import dataclass

IPv4Net = ipaddress.IPv4Network


def wildcard(prefixlen: int) -> str:
    return str(ipaddress.IPv4Address((1 << (32 - prefixlen)) - 1))


def mask(prefixlen: int) -> str:
    return str(IPv4Net(f"0.0.0.0/{prefixlen}").netmask)


def prefix_for_hosts(hosts: int) -> int:
    """Smallest prefix whose usable host count covers `hosts` (plus /31 never; /30 min)."""
    if hosts <= 2:
        return 30
    return 32 - math.ceil(math.log2(hosts + 2))


def subnet_info(cidr: str) -> dict:
    iface = ipaddress.ip_interface(cidr.strip())
    net = iface.network
    if net.version == 6:
        return {"network": str(net), "prefix": net.prefixlen, "first": str(net[0]),
                "addresses": f"2^{128 - net.prefixlen}", "type": _v6_type(net)}
    usable = max(net.num_addresses - 2, 0) if net.prefixlen < 31 else net.num_addresses
    first = net[1] if net.prefixlen < 31 else net[0]
    last = net[-2] if net.prefixlen < 31 else net[-1]
    info = {
        "network": str(net.network_address), "prefix": f"/{net.prefixlen}", "mask": str(net.netmask),
        "wildcard": wildcard(net.prefixlen), "broadcast": str(net.broadcast_address),
        "first_host": str(first), "last_host": str(last), "usable_hosts": usable,
        "class": _klass(net.network_address), "private": net.is_private,
    }
    if iface.ip != net.network_address:
        info["address_is"] = ("network" if iface.ip == net.network_address else
                              "broadcast" if iface.ip == net.broadcast_address and net.prefixlen < 31 else "host")
    return info


def _klass(a: ipaddress.IPv4Address) -> str:
    o = int(str(a).split(".")[0])
    return "A" if o < 128 else "B" if o < 192 else "C" if o < 224 else "D" if o < 240 else "E"


def _v6_type(n) -> str:
    if n.is_link_local:
        return "link-local"
    if n.is_private:
        return "unique-local/private"
    if n.is_multicast:
        return "multicast"
    return "global"


@dataclass
class Alloc:
    name: str
    hosts: int
    network: IPv4Net

    @property
    def gateway(self) -> str:
        return str(self.network[1])

    def host(self, i: int) -> str:
        """i-th usable host (1-based)."""
        return str(self.network[i])

    def row(self) -> dict:
        n = self.network
        return {"name": self.name, "need": self.hosts, "subnet": str(n), "mask": str(n.netmask),
                "wildcard": wildcard(n.prefixlen), "gateway": str(n[1]),
                "range": f"{n[2] if n.num_addresses > 4 else n[1]} - {n[-2]}",
                "broadcast": str(n.broadcast_address), "usable": n.num_addresses - 2}


class Allocator:
    """Carves non-overlapping subnets from a pool, largest-first friendly."""

    def __init__(self, pool: str):
        self.pool = IPv4Net(pool, strict=False)
        self.used: list[IPv4Net] = []

    def take(self, prefixlen: int) -> IPv4Net:
        if prefixlen < self.pool.prefixlen:
            raise ValueError(f"/{prefixlen} does not fit in {self.pool}")
        for cand in self.pool.subnets(new_prefix=prefixlen):
            if not any(cand.overlaps(u) for u in self.used):
                self.used.append(cand)
                return cand
        raise ValueError(f"{self.pool} is exhausted (needed a /{prefixlen})")

    def reserve(self, net: str | IPv4Net) -> None:
        self.used.append(IPv4Net(net, strict=False))


def vlsm(pool: str, needs: dict[str, int] | list[tuple[str, int]]) -> list[Alloc]:
    """Classic VLSM: sort by size desc, allocate sequentially."""
    items = list(needs.items()) if isinstance(needs, dict) else list(needs)
    order = sorted(items, key=lambda kv: -kv[1])
    alloc = Allocator(pool)
    out = {name: Alloc(name, h, alloc.take(prefix_for_hosts(h))) for name, h in order}
    return [out[name] for name, _ in items]


def p2p_links(pool: str, count: int, prefixlen: int = 30) -> list[IPv4Net]:
    alloc = Allocator(pool)
    return [alloc.take(prefixlen) for _ in range(count)]


def summarize(networks: list[str]) -> dict:
    nets = sorted(IPv4Net(n, strict=False) for n in networks)
    collapsed = list(ipaddress.collapse_addresses(nets))
    # single covering supernet
    lo = min(int(n.network_address) for n in nets)
    hi = max(int(n.broadcast_address) for n in nets)
    plen = 32 - (lo ^ hi).bit_length()
    sup = IPv4Net(f"{ipaddress.IPv4Address(lo)}/{plen}", strict=False)
    extra = sup.num_addresses - sum(n.num_addresses for n in collapsed)
    return {"exact_summary": [str(n) for n in collapsed], "single_supernet": str(sup),
            "supernet_mask": str(sup.netmask), "supernet_wildcard": wildcard(sup.prefixlen),
            "extra_addresses_covered": extra}


def same_subnet(a: str, b: str) -> bool:
    ia, ib = ipaddress.ip_interface(a), ipaddress.ip_interface(b)
    return ia.network == ib.network


def split(cidr: str, new_prefix: int | None = None, count: int | None = None) -> list[str]:
    net = IPv4Net(cidr, strict=False)
    if count:
        new_prefix = net.prefixlen + math.ceil(math.log2(count))
    return [str(s) for s in net.subnets(new_prefix=new_prefix)][: count or None]


def eui64(prefix: str, mac: str) -> str:
    hexd = "".join(c for c in mac if c in "0123456789abcdefABCDEF")
    if len(hexd) != 12:
        raise ValueError(f"bad MAC {mac!r}")
    m = [int(hexd[i:i + 2], 16) for i in range(0, 12, 2)]
    m[0] ^= 0x02
    iid = m[:3] + [0xFF, 0xFE] + m[3:]
    net = ipaddress.IPv6Network(prefix, strict=False)
    val = int(net.network_address) | int.from_bytes(bytes(iid), "big")
    return f"{ipaddress.IPv6Address(val)}/{net.prefixlen}"


def table(rows: list[dict], cols: list[str] | None = None) -> str:
    """Render rows as a compact aligned text table."""
    if not rows:
        return "(none)"
    cols = cols or list(rows[0])
    w = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    head = "  ".join(c.ljust(w[c]) for c in cols)
    body = ["  ".join(str(r.get(c, "")).ljust(w[c]) for c in cols) for r in rows]
    return "\n".join([head] + body)
