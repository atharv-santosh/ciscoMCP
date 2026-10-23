"""Tools -> bridge -> fake PT (node) running the real runtime.js."""

import asyncio
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = _free_port()
os.environ["PTMCP_PORT"] = str(PORT)

from ptmcp import server  # noqa: E402  (port must be set first)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


async def call(name, args=None):
    r = await server.mcp.call_tool(name, args or {})
    return "\n".join(c.text for c in r.content if hasattr(c, "text"))


@pytest.fixture
async def pt():
    await server.bridge.start()
    proc = subprocess.Popen(["node", str(Path(__file__).with_name("fake_pt.js")), str(PORT)])
    assert await server.bridge.wait_connected(10)
    yield proc
    proc.kill()
    await server.bridge.stop()


async def test_end_to_end(pt):
    assert "connected" in await call("pt_status")
    out = await call("pt_add_devices", {"devices": [
        {"name": "R1", "model": "2911"}, {"name": "R2", "model": "router"}, {"name": "S1", "model": "2960-24TT"},
        {"name": "PC1", "model": "PC-PT"}, {"name": "PC2", "model": "pc"}]})
    assert "added 5" in out, out
    out = await call("pt_connect", {"links": [{"a": "R1", "b": "R2"}, {"a": "R1", "b": "S1"},
                                              {"a": "S1", "b": "PC1", "a_pref": "access"},
                                              {"a": "S1", "b": "PC2", "a_pref": "access"}]})
    assert out.count("ok ") == 4, out
    assert "R1:GigabitEthernet0/0 <-> R2:GigabitEthernet0/0" in out
    assert "S1:FastEthernet0/1 <-> PC1:FastEthernet0" in out    # access pref keeps gig uplinks free
    assert "S1:GigabitEthernet0/1" in out                     # router uplink took the gig port
    out = await call("pt_auto_address", {"routing": "ospf"})
    assert "192.168.1.0/24" in out and "10.0.0.0/30" in out, out
    assert "0 errors" in out
    topo = await call("pt_topology")
    assert "PC1" in topo and "192.168.1.10/24" in topo and "gw 192.168.1.1" in topo, topo
    assert "No issues" in await call("pt_diagnose")
    out = await call("pt_show", {"device": "R1", "commands": "show version"})
    assert "Cisco IOS Software" in out
    out = await call("pt_cli", {"device": "R1", "commands": ["hostname X", "bogus command"]})
    assert "1 error" in out and "bogus" in out, out
    out = await call("pt_ping", {"pairs": [["PC1", "PC2"], ["PC2", "192.168.1.1"]]})
    assert out.count("OK 2/2") == 2, out
    out = await call("pt_running_config", {"device": "R1", "section": "interface"})
    assert out.startswith("interface"), out
    assert "deleted" in await call("pt_delete_devices", {"names": ["PC2"]})
    out = await call("pt_run_js", {"code": "return ipc.network().getDeviceCount();"})
    assert out.strip() == "4"
    err = await call("pt_cli", {"device": "Nope", "commands": "x"})
    assert err.startswith("ERROR") and "Nope" in err


async def test_lab_build(pt):
    out = await call("pt_lab", {"name": "router-on-a-stick", "params": {"vlans": [10, 20], "pcs_per_vlan": 1}})
    assert "links: 3/3 cabled" in out, out
    assert "config errors: 0" in out, out
    lines = await server.bridge.run("return __fakeLines.filter(l => l[0]==='R1').map(l => l[1]);")
    assert " encapsulation dot1Q 10" in lines and " ip address 192.168.20.1 255.255.255.0" in lines


async def test_not_connected_message():
    out = await call("pt_topology")
    assert "NOT CONNECTED" in out or "ERROR" in out
