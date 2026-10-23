# ciscoMCP

MCP server for Cisco Packet Tracer 9. Lets Claude Code (or any MCP client) build networks in a running Packet Tracer: add devices, cable them, push IOS configs, address everything, set up routing, ping, and find what's broken. It can also read and write .pkt files without Packet Tracer open.

There are also some ready-made labs (router on a stick, OSPF/EIGRP/RIP topologies, NAT, HSRP, EtherChannel, IPv6) that build in one call.

## Install

Needs Python 3.10+, [uv](https://docs.astral.sh/uv/) and Packet Tracer 9.

```
git clone https://github.com/atharv-santosh/ciscoMCP
cd ciscoMCP
uv sync
claude mcp add packet-tracer -- uv --directory "$PWD" run packet-tracer-mcp
```

Then in Packet Tracer: Extensions > Scripting > Configure PT Script Modules > Add, pick `pt_extension/ptmcp.pts` and restart PT. A small "PT-MCP Bridge" window shows up and goes green once the MCP server is running.

If something isn't connecting, run `uv run ptmcp doctor`.

## Use

Open Packet Tracer, start Claude Code and ask for things like:

- build a 3 router OSPF ring with a PC on each LAN and ping everything
- set up router on a stick with VLANs 10, 20, 30 and DHCP
- what's wrong with this network
- show me the running config on R1

`examples/enterprise-two-site.pkt` is a bigger network built entirely through the tools.

## Tests

```
uv run pytest
```
