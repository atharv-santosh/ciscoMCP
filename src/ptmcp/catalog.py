"""Device, module and cable catalog for PT 9. Type codes are the DeviceType enum."""

from __future__ import annotations

from dataclasses import dataclass, field

# DeviceType enum (PT 8.2+/9.0)
ROUTER, SWITCH, CLOUD, BRIDGE, HUB, REPEATER, SPLITTER, ACCESS_POINT, PC, SERVER, PRINTER = range(11)
WIRELESS_ROUTER, IP_PHONE, DSL_MODEM, CABLE_MODEM, REMOTE_NET, L3_SWITCH = 11, 12, 13, 14, 15, 16
SWITCH_3650, LAPTOP, TABLET, PDA, WIRELESS_END, WIRED_END, TV, HOME_VOIP, ANALOG_PHONE = 17, 18, 19, 20, 21, 22, 23, 24, 25
ASA, IOE, HOME_GATEWAY, CELL_TOWER, CO_SERVER, SNIFFER, MCU, SBC, THING = 27, 28, 29, 31, 32, 35, 36, 37, 38
EMBEDDED_SERVER, WLC, LAP, MERAKI_MX, MERAKI_SERVER, NET_CONTROLLER = 40, 41, 44, 48, 49, 50

TYPE_NAMES = {
    0: "router", 1: "switch", 2: "cloud", 3: "bridge", 4: "hub", 5: "repeater", 6: "coax-splitter",
    7: "access-point", 8: "pc", 9: "server", 10: "printer", 11: "wireless-router", 12: "ip-phone",
    13: "dsl-modem", 14: "cable-modem", 15: "remote-network", 16: "l3-switch", 17: "l3-switch",
    18: "laptop", 19: "tablet", 20: "smartphone", 21: "wireless-end-device", 22: "wired-end-device",
    23: "tv", 24: "home-voip", 25: "analog-phone", 26: "multiuser", 27: "asa", 28: "iot",
    29: "home-gateway", 30: "home-router", 31: "cell-tower", 32: "central-office-server",
    33: "cisco-ap", 34: "embedded-ap", 35: "sniffer", 36: "mcu", 37: "sbc", 38: "thing",
    40: "embedded-server", 41: "wlc", 44: "lightweight-ap", 45: "power-distribution",
    46: "patch-panel", 47: "wall-mount", 48: "meraki-mx", 49: "meraki-server", 50: "network-controller",
}

CLI_TYPES = {0, 1, 16, 17, 27}            # devices with an IOS/ASA CLI
HOST_TYPES = {8, 9, 10, 18, 19, 20, 21, 22, 12}  # devices configured through IP settings
L2_TYPES = {1, 4, 3, 5, 7, 33, 44}        # forward frames without routing (switch-like)
L3_SWITCH_TYPES = {16, 17}


def _r(n):
    return [f"GigabitEthernet0/{i}" for i in range(n)]


@dataclass(frozen=True)
class Model:
    model: str
    types: tuple[int, ...]
    role: str
    ports: tuple[str, ...] = ()
    slots: str = ""
    notes: str = ""
    aliases: tuple[str, ...] = field(default=())


def _fa(prefix, a, b):
    return tuple(f"{prefix}{i}" for i in range(a, b + 1))


MODELS: dict[str, Model] = {m.model.lower(): m for m in [
    # routers
    Model("1941", (ROUTER,), "router", ("GigabitEthernet0/0", "GigabitEthernet0/1"),
          "2x EHWIC: slot 0/0 and 0/1 (HWIC-2T serial, HWIC-4ESW, EHWIC-...)", "Classic CCNA lab router; IOS 15.1.", ("r1941",)),
    Model("2901", (ROUTER,), "router", ("GigabitEthernet0/0", "GigabitEthernet0/1"),
          "4x EHWIC (0/0..0/3)", "Like 1941 with more slots."),
    Model("2911", (ROUTER,), "router", ("GigabitEthernet0/0", "GigabitEthernet0/1", "GigabitEthernet0/2"),
          "4x EHWIC (0/0..0/3), 1x SM", "Best default router: 3 gig ports, security + voice licenses.", ("router", "r2911")),
    Model("ISR4321", (ROUTER,), "router", ("GigabitEthernet0/0/0", "GigabitEthernet0/0/1"),
          "2x NIM (0/1, 0/2) e.g. NIM-2T", "IOS-XE; port names have three parts.", ("4321",)),
    Model("ISR4331", (ROUTER,), "router", ("GigabitEthernet0/0/0", "GigabitEthernet0/0/1", "GigabitEthernet0/0/2"),
          "3x NIM (0/1..0/3)", "IOS-XE; current CCNA curriculum router.", ("4331",)),
    Model("1841", (ROUTER,), "router", ("FastEthernet0/0", "FastEthernet0/1"), "2x HWIC/WIC (0/0, 0/1)",
          "Old 12.4 router; FastEthernet only."),
    Model("2811", (ROUTER,), "router", ("FastEthernet0/0", "FastEthernet0/1"), "4x HWIC, 1x NM",
          "Old 12.4 router."),
    Model("2620XM", (ROUTER,), "router", ("FastEthernet0/0",), "1x NM, 2x WIC", "Legacy."),
    Model("2621XM", (ROUTER,), "router", ("FastEthernet0/0", "FastEthernet0/1"), "1x NM, 2x WIC", "Legacy."),
    Model("819HGW", (ROUTER,), "router", (), "", "Integrated 4G/wifi router."),
    Model("829", (ROUTER,), "router", (), "", "Industrial ISR."),
    Model("CGR1240", (ROUTER,), "router", (), "", "Connected grid router."),
    Model("IR8340", (ROUTER,), "router", (), "", "Industrial router (PT 9)."),
    Model("PT8200", (ROUTER,), "router", (), "", "PT 9 generic modern router."),
    Model("Router-PT", (ROUTER,), "router",
          ("FastEthernet0/0", "FastEthernet1/0", "Serial2/0", "Serial3/0", "FastEthernet4/0", "FastEthernet5/0"),
          "10 PT-ROUTER-NM slots", "Generic router with built-in serial ports: quickest for serial WAN labs."),
    Model("Router-PT-Empty", (ROUTER,), "router", (), "10 empty PT-ROUTER-NM slots", "Generic, no interfaces."),
    # switches
    Model("2960-24TT", (SWITCH,), "switch", _fa("FastEthernet0/", 1, 24) + ("GigabitEthernet0/1", "GigabitEthernet0/2"),
          "", "Default access switch: 24 Fa + 2 Gi uplinks.", ("switch", "2960")),
    Model("2950-24", (SWITCH,), "switch", _fa("FastEthernet0/", 1, 24), "", "Legacy L2."),
    Model("2950T-24", (SWITCH,), "switch", _fa("FastEthernet0/", 1, 24) + ("GigabitEthernet0/1", "GigabitEthernet0/2"), "", "Legacy L2."),
    Model("Switch-PT", (SWITCH,), "switch", ("FastEthernet0/1", "FastEthernet1/1", "FastEthernet2/1", "FastEthernet3/1",
                                               "FastEthernet4/1", "FastEthernet5/1"), "10 PT-SWITCH-NM slots", "Generic."),
    Model("Switch-PT-Empty", (SWITCH,), "switch", (), "", "Generic, no interfaces."),
    Model("IE-2000", (L3_SWITCH, SWITCH), "switch", (), "", "Industrial Ethernet switch."),
    Model("3560-24PS", (L3_SWITCH,), "l3-switch", _fa("FastEthernet0/", 1, 24) + ("GigabitEthernet0/1", "GigabitEthernet0/2"),
          "", "Layer-3 switch: SVIs, ip routing, EtherChannel, HSRP. Use for inter-VLAN routing.", ("multilayer", "l3switch", "3560")),
    Model("3650-24PS", (SWITCH_3650, L3_SWITCH), "l3-switch",
          tuple(f"GigabitEthernet1/0/{i}" for i in range(1, 25)) + tuple(f"GigabitEthernet1/1/{i}" for i in range(1, 5)),
          "1 uplink module slot", "IOS-XE L3 switch. Ships WITHOUT a power supply in PT: add AC-POWER-SUPPLY.", ("3650",)),
    # end devices
    Model("PC-PT", (PC,), "host", ("FastEthernet0",), "1 host NM slot", "Desktop PC.", ("pc",)),
    Model("Laptop-PT", (LAPTOP,), "host", ("FastEthernet0",), "1 laptop NM slot (swap for WPC300N for wifi)", "", ("laptop",)),
    Model("Server-PT", (SERVER,), "host", ("FastEthernet0",), "", "DHCP/DNS/HTTP/FTP/Email/AAA/NTP/Syslog services.", ("server",)),
    Model("Printer-PT", (PRINTER,), "host", ("FastEthernet0",), "", "", ("printer",)),
    Model("TabletPC-PT", (TABLET,), "host", ("Wireless0",), "", "Wireless by default.", ("tablet",)),
    Model("SMARTPHONE-PT", (PDA,), "host", ("Wireless0",), "", "Wireless + 3G/4G.", ("smartphone", "phone-pt")),
    Model("7960", (IP_PHONE,), "host", ("Switch", "PC"), "", "Cisco IP phone; needs power adapter or PoE + voice VLAN.", ("ipphone",)),
    Model("TV-PT", (TV,), "host", (), "", ""),
    Model("WirelessEndDevice-PT", (WIRELESS_END,), "host", (), "", ""),
    Model("WiredEndDevice-PT", (WIRED_END,), "host", (), "", ""),
    Model("Sniffer", (SNIFFER,), "host", (), "", "Captures traffic on a link."),
    # other
    Model("Hub-PT", (HUB,), "hub", (), "", "", ("hub",)),
    Model("Repeater-PT", (REPEATER,), "hub", (), "", ""),
    Model("Bridge-PT", (BRIDGE,), "switch", (), "", ""),
    Model("AccessPoint-PT", (ACCESS_POINT,), "ap", (), "", "Autonomous AP (802.11g)."),
    Model("AccessPoint-PT-N", (ACCESS_POINT,), "ap", (), "", "802.11n AP."),
    Model("AccessPoint-PT-AC", (ACCESS_POINT,), "ap", (), "", "802.11ac AP."),
    Model("Linksys-WRT300N", (WIRELESS_ROUTER,), "wireless-router", (), "", "Home wireless router with GUI config."),
    Model("HomeRouter-PT-AC", (30,), "wireless-router", (), "", ""),
    Model("WLC-2504", (WLC,), "wlc", (), "", "Wireless LAN controller."),
    Model("WLC-3504", (WLC,), "wlc", (), "", ""),
    Model("LAP-PT", (LAP,), "ap", (), "", "Lightweight AP (needs WLC)."),
    Model("3702i", (LAP,), "ap", (), "", "Lightweight AP."),
    Model("5506-X", (ASA,), "firewall", tuple(f"GigabitEthernet1/{i}" for i in range(1, 9)), "",
          "ASA firewall (ASA OS CLI, not IOS).", ("asa",)),
    Model("5505", (ASA,), "firewall", tuple(f"Ethernet0/{i}" for i in range(0, 8)), "", "Legacy ASA with switch ports."),
    Model("Cloud-PT", (CLOUD,), "cloud", (), "", "WAN cloud (frame relay / DSL / cable)."),
    Model("DSL-Modem-PT", (DSL_MODEM,), "modem", (), "", ""),
    Model("Cable-Modem-PT", (CABLE_MODEM,), "modem", (), "", ""),
    Model("Cell-Tower", (CELL_TOWER,), "cellular", (), "", ""),
    Model("Central-Office-Server", (CO_SERVER,), "cellular", (), "", ""),
    Model("Meraki-MX65W", (MERAKI_MX,), "firewall", (), "", ""),
    Model("NetworkController", (NET_CONTROLLER,), "controller", (), "", "SDN controller with REST API."),
    Model("MCU-PT", (MCU,), "iot", (), "", "Microcontroller (programmable)."),
    Model("SBC-PT", (SBC,), "iot", (), "", "Single-board computer (programmable)."),
]}

ALIASES = {a.lower(): m for m in MODELS.values() for a in m.aliases}


def resolve_model(name: str) -> Model:
    key = name.strip().lower()
    if key in MODELS:
        return MODELS[key]
    if key in ALIASES:
        return ALIASES[key]
    # anything else: try it as an IoE thing
    raise KeyError(name)


def model_or_passthrough(name: str) -> tuple[str, list[int]]:
    """Return (exact PT model name, type codes to try)."""
    try:
        m = resolve_model(name)
        return m.model, list(m.types)
    except KeyError:
        return name, [THING, IOE, 39]


# module type codes (PT ModuleType enum)
MODULE_TYPES: dict[str, int] = {}
for _names, _code in [
    ("NM-1E NM-1E2W NM-1FE-FX NM-1FE-TX NM-1FE2W NM-2E2W NM-2FE2W NM-2W NM-4A/S NM-4E NM-8A/S NM-8AM NM-Cover NM-ESW-161", 1),
    ("1240-Cellular 1240-Cover HWIC-1GE-SFP HWIC-2T HWIC-4ESW HWIC-8A HWIC-AP-AG-B NIM-2T NIM-Cover NIM-ES2-4 "
     "WIC-1AM WIC-1ENET WIC-1T WIC-2AM WIC-2T WIC-Cover", 2),
    ("PT-ROUTER-NM-1AM PT-ROUTER-NM-1CE PT-ROUTER-NM-1CFE PT-ROUTER-NM-1CGE PT-ROUTER-NM-1FFE PT-ROUTER-NM-1FGE "
     "PT-ROUTER-NM-1S PT-ROUTER-NM-1SS PT-ROUTER-NM-COVER", 3),
    ("AC-POWER-SUPPLY POWER-COVER-PLATE PT-SWITCH-NM-1CE PT-SWITCH-NM-1CFE PT-SWITCH-NM-1CGE PT-SWITCH-NM-1FFE "
     "PT-SWITCH-NM-1FGE PT-SWITCH-NM-COVER", 4),
    ("PT-CLOUD-NM-1AM PT-CLOUD-NM-1CE PT-CLOUD-NM-1CFE PT-CLOUD-NM-1CGE PT-CLOUD-NM-1FFE PT-CLOUD-NM-1FGE PT-CLOUD-NM-1S", 5),
    ("PT-REPEATER-NM-1CE PT-REPEATER-NM-1CFE PT-REPEATER-NM-1CGE PT-REPEATER-NM-1FFE PT-REPEATER-NM-1FGE PT-REPEATER-NM-COVER", 6),
    ("Linksys-WMP300N PT-HOST-NM-1AM PT-HOST-NM-1CE PT-HOST-NM-1CFE PT-HOST-NM-1CGE PT-HOST-NM-1FFE PT-HOST-NM-1FGE "
     "PT-HOST-NM-1W PT-HOST-NM-1W-A PT-HOST-NM-1W-AC PT-HOST-NM-3G/4G PT-HOST-NM-COVER", 7),
    ("PT-MODEM-NM-1CE PT-MODEM-NM-1CFE PT-MODEM-NM-1CGE", 8),
    ("Linksys-WPC300N PT-LAPTOP-NM-1AM PT-LAPTOP-NM-1CE PT-LAPTOP-NM-1CFE PT-LAPTOP-NM-1CGE PT-LAPTOP-NM-1FFE "
     "PT-LAPTOP-NM-1FGE PT-LAPTOP-NM-1W PT-LAPTOP-NM-1W-A PT-LAPTOP-NM-1W-AC PT-LAPTOP-NM-3G/4G", 9),
    ("IP_PHONE_POWER_ADAPTER", 11),
    ("PT-HEADPHONE PT-MICROPHONE", 16),
    ("ASA-Cover", 19),
    ("PT-CELL-NM-1CX PT-CELL-NM-3G/4G", 21),
    ("PT-IOT-CUSTOM-IO", 26),
    ("PT-IOT-POWER-ADAPTER", 27),
    ("PT-UNV-PWR-ADAPTER", 28),
    ("ROUTER-ADAPTER", 29),
    ("GLC-FE-100FX-RGD GLC-GE-100FX GLC-LH-SMD GLC-T GLC-TE", 30),
    ("ACCESS_POINT_POWER_ADAPTER", 31),
    ("C3650-BUILTIN C3650-SFP-BUILTIN ISR4321-BUILTIN ISR4331-BUILTIN PT-CONTROLLER-BUILTIN", 32),
    ("MERAKI-POWER-ADAPTER", 34),
]:
    for _n in _names.split():
        MODULE_TYPES.setdefault(_n, _code)

MODULE_NOTES = {
    "HWIC-2T": "2 smart-serial WAN ports for 1941/2901/2911 (slot 0/0..0/3) -> Serial0/0/0, Serial0/0/1",
    "WIC-2T": "2 serial ports for 1841/2811 -> Serial0/0/0, Serial0/0/1",
    "NIM-2T": "2 serial ports for ISR4321/4331 (slot 0/1, 0/2) -> Serial0/1/0, Serial0/1/1",
    "HWIC-4ESW": "4-port switch module (VLAN-capable) for ISR G2",
    "NIM-ES2-4": "4-port switch module for ISR4K",
    "HWIC-1GE-SFP": "1 SFP gigabit port",
    "NM-1FE-TX": "1 FastEthernet for 2811/2620",
    "PT-ROUTER-NM-1S": "1 serial port for Router-PT",
    "PT-ROUTER-NM-1CGE": "1 copper gig for Router-PT",
    "AC-POWER-SUPPLY": "power supply for 3650-24PS (slot is the PSU bay)",
    "Linksys-WPC300N": "wireless NIC for Laptop-PT/PC-PT (replace the wired NM)",
    "PT-HOST-NM-1W-AC": "802.11ac NIC for PC/Laptop",
}

CABLES = {
    "auto": "PT picks the right copper/serial cable (default)",
    "straight": "copper straight-through: unlike devices (PC-switch, router-switch)",
    "cross": "copper crossover: like devices (switch-switch, router-router, PC-PC, router-PC)",
    "serial": "serial DCE/DTE (first device is DCE -> set clock rate there)",
    "fiber": "fiber between fiber/SFP ports",
    "console": "rollover console cable PC RS232 -> device Console",
    "roll": "rollover", "phone": "RJ11 phone", "coaxial": "coax", "usb": "usb", "wireless": "wireless",
    "octal": "octal", "cellular": "cellular", "cable": "cable modem",
}


def catalog_text(category: str | None = None) -> str:
    rows = []
    for m in MODELS.values():
        if category and category.lower() not in (m.role, TYPE_NAMES.get(m.types[0], "")):
            continue
        ports = ""
        if m.ports:
            ports = (", ".join(m.ports) if len(m.ports) <= 6
                     else f"{m.ports[0]}..{m.ports[-1]} ({len(m.ports)} ports)")
        line = f"{m.model} [{m.role}]"
        if ports:
            line += f" ports: {ports}"
        if m.slots:
            line += f" | slots: {m.slots}"
        if m.notes:
            line += f" | {m.notes}"
        rows.append(line)
    return "\n".join(rows)
