import glob

import pytest

from tracerlab import catalog, configgen as cg, labs, netcalc, pkt, planner
from tracerlab.twofish import EAX, Twofish

from fixtures import two_router_snapshot


def test_twofish_vectors():
    assert Twofish(bytes(16)).encrypt_block(bytes(16)).hex().upper() == "9F589F5CF6122C32B6BFEC2F2AE8C35A"
    # 256-bit key vector from the Twofish paper
    k = bytes.fromhex("0123456789ABCDEFFEDCBA987654321000112233445566778899AABBCCDDEEFF")
    assert Twofish(k).encrypt_block(bytes(16)).hex().upper() == "37527BE0052334B89F0CFCCAE87CFA20"
    assert Twofish(k[:24]).encrypt_block(bytes(16)).hex().upper() == "CFD1D2E5A9BE9CDF501F13B892BD2248"


def test_eax_roundtrip():
    e = EAX(b"k" * 16)
    ct = e.encrypt(b"n" * 16, b"hello packet tracer" * 5)
    assert e.decrypt(b"n" * 16, ct) == b"hello packet tracer" * 5
    with pytest.raises(ValueError):
        e.decrypt(b"n" * 16, ct[:-1] + bytes([ct[-1] ^ 1]))


def test_pkt_roundtrip_xml():
    xml = b"<PACKETTRACER5><VERSION>9.0.0</VERSION><NETWORK><DEVICES/><LINKS/></NETWORK></PACKETTRACER5>"
    assert pkt.decrypt(pkt.encrypt(xml)) == xml
    info = pkt.parse(xml.decode())
    assert info["version"] == "9.0.0" and info["devices"] == []


SAMPLES = sorted(glob.glob("/Applications/Cisco Packet Tracer*/**/saves/01 Networking/LLDP/*.pkt", recursive=True))


@pytest.mark.skipif(not SAMPLES, reason="Packet Tracer sample files not installed")
def test_pkt_real_file():
    info = pkt.inspect(SAMPLES[0])
    assert len(info["devices"]) >= 2 and info["links"]
    assert all(l["a"] and l["b"] for l in info["links"])


def test_subnet_and_vlsm():
    i = netcalc.subnet_info("172.16.5.77/20")
    assert i["network"] == "172.16.0.0" and i["broadcast"] == "172.16.15.255" and i["usable_hosts"] == 4094
    assert i["wildcard"] == "0.0.15.255"
    plan = netcalc.vlsm("10.0.0.0/24", {"A": 100, "B": 50, "C": 2})
    assert [str(a.network) for a in plan] == ["10.0.0.0/25", "10.0.0.128/26", "10.0.0.192/30"]
    s = netcalc.summarize(["10.1.0.0/24", "10.1.1.0/24", "10.1.2.0/24", "10.1.3.0/24"])
    assert s["exact_summary"] == ["10.1.0.0/22"] and s["single_supernet"] == "10.1.0.0/22"
    assert netcalc.eui64("2001:db8::/64", "0060.47AB.CDEF") == "2001:db8::260:47ff:feab:cdef/64"


def test_configgen_core():
    o = cg.ospf(1, ["10.0.0.0/30", {"net": "192.168.1.0/24", "area": 1}], router_id="1.1.1.1", passive=["g0/0"])
    assert " network 10.0.0.0 0.0.0.3 area 0" in o and " network 192.168.1.0 0.0.0.255 area 1" in o
    ap = cg.access_ports("Fa0/1 - 3, Fa0/7", 10)
    assert [l for l in ap if l.startswith("interface")] == ["interface Fa0/1", "interface Fa0/2", "interface Fa0/3",
                                                             "interface Fa0/7"]
    assert not any("range" in l for l in cg.etherchannel("Fa0/23 - 24", 1) + cg.trunk(["Gi0/1", "Gi0/2"]))
    assert cg.trunk("Gi0/1", allowed=[10, 20], dot1q_encap=True)[1:3] == [" switchport trunk encapsulation dot1q",
                                                                          " switchport mode trunk"]
    acl = cg.acl("WEB", [{"action": "permit", "proto": "tcp", "src": "10.0.0.0/24", "dst": "host 1.1.1.1", "port": 80}],
                 apply=[{"interface": "g0/0", "dir": "in"}])
    assert acl[:2] == ["ip access-list extended WEB", " permit tcp 10.0.0.0 0.0.0.255 host 1.1.1.1 eq 80"]
    assert cg.rip(["10.1.1.0/30", "10.2.2.0/30", "172.16.1.0/24"])[2:4] == [" network 10.0.0.0", " network 172.16.0.0"]
    nat = cg.nat_overload(["g0/0"], "g0/1", ["192.168.1.0/24"])
    assert "ip nat inside source list 1 interface g0/1 overload" in nat
    d = cg.dhcp_pool("LAN", "192.168.1.0/24", dns="8.8.8.8", exclude_first=10)
    assert d[0] == "ip dhcp excluded-address 192.168.1.1 192.168.1.10" and " default-router 192.168.1.1" in d
    for name in cg.FEATURES:
        assert name in cg.feature_help()


def test_planner_auto_address_and_static():
    t = planner.Topology(two_router_snapshot())
    p = planner.auto_address(t, routing="static")
    nets = [s["subnet"] for s in p.subnets]
    assert nets == ["192.168.1.0/24", "10.0.0.0/30", "192.168.2.0/24"]
    assert "ip route 192.168.2.0 255.255.255.0 10.0.0.2" in p.device_cmds["R1"]
    assert "ip route 192.168.1.0 255.255.255.0 10.0.0.1" in p.device_cmds["R2"]
    assert p.host_cfg["PC3"]["gw"] == "192.168.2.1"
    pd = planner.auto_address(t, routing="eigrp", dhcp=True)
    assert any(l.startswith("ip dhcp pool") for l in pd.device_cmds["R1"])
    assert pd.host_cfg["PC1"] == {"port": "FastEthernet0", "dhcp": True}


def test_planner_keeps_existing_and_diagnoses():
    snap = two_router_snapshot(addressed=True)
    snap["devices"][5]["gw"] = "192.168.1.254"                 # PC2 wrong gateway
    snap["devices"][6]["ports"][0][1] = "192.168.3.10/24"      # PC3 wrong subnet
    t = planner.Topology(snap)
    issues = planner.diagnose(t)
    assert any("PC2: default gateway 192.168.1.254" in i for i in issues)
    assert any("mixed subnets" in i and "PC3" in i for i in issues)
    p = planner.auto_address(t, routing="none")
    assert all(s["kind"] == "kept" for s in p.subnets)


def test_catalog_and_labs():
    assert catalog.resolve_model("router").model == "2911"
    assert catalog.model_or_passthrough("Fan")[1][0] == catalog.THING
    assert catalog.MODULE_TYPES["HWIC-2T"] == 2
    for name, fn in labs.LABS.items():
        L = fn()
        names = {d["name"] for d in L.devices}
        assert len(names) == len(L.devices), name
        for l in L.links:
            assert l["a"] in names and l["b"] in names, (name, l)
        for dev in L.cmds:
            assert dev in names, (name, dev)
    big = labs.routing("eigrp", 4, "mesh")
    assert {d["model"] for d in big.devices if d["name"].startswith("R")} == {"Router-PT"}


def _enterprise():
    import json
    import pathlib
    d = json.loads((pathlib.Path(__file__).parent / "data" / "enterprise_live.json").read_text())
    return d["snap"], d["cfg"]


def test_planner_vlan_aware_segments_live_capture():
    # captured from PT 9 (examples/enterprise-two-site.pkt)
    snap, cfg = _enterprise()
    t = planner.Topology(snap, cfg)
    segs = {tuple(sorted(s["endpoints"])) for s in t.segments()}
    assert (("CORE", "Vlan10"), ("PC1", "FastEthernet0"), ("PC3", "FastEthernet0")) in segs
    assert (("CORE", "Vlan30"), ("PC4", "FastEthernet0")) in segs
    assert (("CORE", "GigabitEthernet0/1"), ("EDGE", "GigabitEthernet0/1")) in segs   # routed port
    assert t.port("CORE", "GigabitEthernet0/1").ip == "10.0.0.2/30"                    # IP read from config
    assert [i for i in planner.diagnose(t) if "ACL" not in i] == []
    # without configs every switch port is in VLAN 1
    assert any("mixed subnets" in i for i in planner.diagnose(planner.Topology(snap)))


def test_planner_vlan_misconfig_detected():
    snap, cfg = _enterprise()
    cfg = dict(cfg)
    cfg["ACC1"] = cfg["ACC1"].replace("interface FastEthernet0/1\n switchport access vlan 10",
                                      "interface FastEthernet0/1\n switchport access vlan 20")
    assert cfg["ACC1"] != _enterprise()[1]["ACC1"]
    issues = planner.diagnose(planner.Topology(snap, cfg))
    assert any(i.startswith("mixed subnets") and "PC1" in i for i in issues), issues
    assert any(i.startswith("PC1: default gateway") for i in issues), issues
    # trunk that no longer allows VLAN 10 isolates PC3 from its gateway
    cfg = dict(_enterprise()[1])
    cfg["ACC2"] = cfg["ACC2"].replace("switchport trunk allowed vlan 10,20,30,50", "switchport trunk allowed vlan 20,30,50")
    issues = planner.diagnose(planner.Topology(snap, cfg))
    assert any(i.startswith("PC3: default gateway 192.168.10.1 unreachable") for i in issues), issues


def test_parse_interfaces():
    ifs = planner.parse_interfaces("interface GigabitEthernet0/0.10\n encapsulation dot1Q 10\n ip address 10.1.1.1 255.255.255.0\n"
                                   "interface FastEthernet0/1\n switchport trunk allowed vlan 10-12,20\n switchport trunk allowed vlan add 30\n"
                                   " switchport mode trunk\n channel-group 1 mode active\n")
    assert ifs["GigabitEthernet0/0.10"]["dot1q"] == 10 and ifs["GigabitEthernet0/0.10"]["ip"] == "10.1.1.1/24"
    assert ifs["FastEthernet0/1"]["allowed"] == {10, 11, 12, 20, 30} and ifs["FastEthernet0/1"]["group"] == 1


def test_unbound_acl_detected():
    # PT doesn't write SVI access-groups to the config, so this one reads as unbound
    snap, cfg = _enterprise()
    issues = planner.unbound_acls(planner.Topology(snap, cfg))
    assert any(i.startswith("CORE: ACL GUEST-ISOLATE is defined but not applied") for i in issues), issues
    assert not any("EDGE" in i for i in issues)     # access-list 1 is used by NAT
