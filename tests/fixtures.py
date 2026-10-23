"""Synthetic PTM.snapshot() payloads for offline tests."""


def dev(n, m, t, ports, x=0, y=0, **kw):
    return {"n": n, "m": m, "t": t, "x": x, "y": y, "on": 1, "ports": ports, **kw}


def port(name, ip="", peer="", peer_port="", up=2, ptype=4):
    return [name, ip, peer, peer_port, up if peer else 0, ptype]


def two_router_snapshot(addressed=False):
    """PC1,PC2 - S1 - R1 ==== R2 - S2 - PC3"""
    ip = (lambda v: v) if addressed else (lambda v: "")
    devices = [
        dev("R1", "2911", 0, [port("GigabitEthernet0/0", ip("192.168.1.1/24"), "S1", "GigabitEthernet0/1"),
                              port("GigabitEthernet0/1", ip("10.0.0.1/30"), "R2", "GigabitEthernet0/1"),
                              port("GigabitEthernet0/2")]),
        dev("R2", "2911", 0, [port("GigabitEthernet0/0", ip("192.168.2.1/24"), "S2", "GigabitEthernet0/1"),
                              port("GigabitEthernet0/1", ip("10.0.0.2/30"), "R1", "GigabitEthernet0/1")]),
        dev("S1", "2960-24TT", 1, [port("GigabitEthernet0/1", "", "R1", "GigabitEthernet0/0"),
                                   port("FastEthernet0/1", "", "PC1", "FastEthernet0", ptype=3),
                                   port("FastEthernet0/2", "", "PC2", "FastEthernet0", ptype=3)]),
        dev("S2", "2960-24TT", 1, [port("GigabitEthernet0/1", "", "R2", "GigabitEthernet0/0"),
                                   port("FastEthernet0/1", "", "PC3", "FastEthernet0", ptype=3)]),
        dev("PC1", "PC-PT", 8, [port("FastEthernet0", ip("192.168.1.10/24"), "S1", "FastEthernet0/1", ptype=3)],
            gw="192.168.1.1" if addressed else ""),
        dev("PC2", "PC-PT", 8, [port("FastEthernet0", ip("192.168.1.11/24"), "S1", "FastEthernet0/2", ptype=3)],
            gw="192.168.1.1" if addressed else ""),
        dev("PC3", "PC-PT", 8, [port("FastEthernet0", ip("192.168.2.10/24"), "S2", "FastEthernet0/1", ptype=3)],
            gw="192.168.2.1" if addressed else ""),
    ]
    links = [["R1", "GigabitEthernet0/0", "S1", "GigabitEthernet0/1", "straight"],
             ["R1", "GigabitEthernet0/1", "R2", "GigabitEthernet0/1", "cross"],
             ["R2", "GigabitEthernet0/0", "S2", "GigabitEthernet0/1", "straight"],
             ["S1", "FastEthernet0/1", "PC1", "FastEthernet0", "straight"],
             ["S1", "FastEthernet0/2", "PC2", "FastEthernet0", "straight"],
             ["S2", "FastEthernet0/1", "PC3", "FastEthernet0", "straight"]]
    return {"devices": devices, "links": links}
