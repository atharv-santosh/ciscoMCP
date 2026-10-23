// PTM runtime, pushed into PT's script engine by the MCP server.
// Jobs run as new Function(body), so everything lives on the global PTM object.
var __g = (typeof globalThis !== "undefined") ? globalThis : (function () { return this; })();
if (typeof __g.PTM !== "object" || !__g.PTM) { __g.PTM = {}; }

(function (P) {
    var CONNECT = { straight: 8100, cross: 8101, roll: 8102, fiber: 8103, phone: 8104, cable: 8105,
                    serial: 8106, auto: 8107, console: 8108, wireless: 8109, coaxial: 8110, octal: 8111,
                    cellular: 8112, usb: 8113, custom_io: 8114 };
    var CONNECT_NAME = {};
    for (var k in CONNECT) { CONNECT_NAME[CONNECT[k]] = k; }
    P.CONNECT = CONNECT;

    // PortType codes
    var PT_ETH = { 2: 1, 3: 1, 4: 1, 5: 1, 6: 1, 32: 1 };
    var PT_FIBER = { 5: 1, 6: 1 };
    var PT_SERIAL = { 7: 1, 8: 1 };
    var PT_LOGICAL = { 14: 1, 15: 1, 16: 1, 17: 1, 20: 1, 25: 1, 26: 1, 28: 1, 34: 1, 40: 1 };
    var CLI_TYPES = { 0: 1, 1: 1, 16: 1, 17: 1, 27: 1 }; // router, switch, L3 switch, 3650, ASA
    var HIDDEN_TYPES = { 45: 1 };  // hidden Power Distribution Device (PT 9)
    var HOST_TYPES = { 8: 1, 9: 1, 10: 1, 12: 1, 18: 1, 19: 1, 20: 1, 21: 1, 22: 1 };
    // MDI-X devices (switches, hubs, bridges, APs)
    var MDIX_TYPES = { 1: 1, 3: 1, 4: 1, 5: 1, 7: 1, 16: 1, 17: 1, 33: 1, 44: 1 };

    function net() { return ipc.network(); }
    function lw() { return ipc.appWindow().getActiveWorkspace().getLogicalWorkspace(); }
    function s(v) { return v === null || v === undefined ? "" : String(v); }

    function dev(name) {
        var d = net().getDevice(name);
        if (!d) { throw new Error("no device named '" + name + "' (names are case-sensitive)"); }
        return d;
    }
    P.dev = dev;

    function tryCall(obj, fn, dflt) {
        try { var v = obj[fn](); return v === undefined ? dflt : v; } catch (e) { return dflt; }
    }

    function ipOf(port) {
        var ip = s(tryCall(port, "getIpAddress", ""));
        if (!ip || ip === "0.0.0.0") { return ""; }
        var mask = s(tryCall(port, "getSubnetMask", ""));
        return ip + "/" + maskBits(mask);
    }

    function maskBits(mask) {
        var parts = s(mask).split("."), bits = 0;
        for (var i = 0; i < parts.length; i++) {
            var n = parseInt(parts[i], 10) || 0;
            while (n) { bits += n & 1; n >>= 1; }
        }
        return bits;
    }

    function linkPeer(port) {
        var link = tryCall(port, "getLink", null);
        if (!link) { return null; }
        try {
            var p1 = link.getPort1(), p2 = link.getPort2();
            var other = (p1 && p1.getName() === port.getName() && p1.getOwnerDevice().getName() === port.getOwnerDevice().getName()) ? p2 : p1;
            return { dev: other.getOwnerDevice().getName(), port: other.getName(), type: CONNECT_NAME[link.getConnectionType()] || s(link.getConnectionType()) };
        } catch (e) { return { dev: "?", port: s(tryCall(port, "getRemotePortName", "?")) }; }
    }

    function eachPort(d, fn) {
        var n = d.getPortCount();
        for (var i = 0; i < n; i++) {
            var p = d.getPortAt(i);
            if (p) { fn(p, i); }
        }
    }

    // ports: [name, ip/len, peerDev, peerPort, up, typeCode, extra]
    P.snapshot = function (opts) {
        opts = opts || {};
        var out = { devices: [], links: [] };
        var n = net().getDeviceCount();
        for (var i = 0; i < n; i++) {
            var d = net().getDeviceAt(i);
            if (HIDDEN_TYPES[d.getType()]) { continue; }
            var rec = { n: d.getName(), m: s(d.getModel()), t: d.getType(),
                        x: Math.round(tryCall(d, "getCenterXCoordinate", tryCall(d, "getXCoordinate", 0))),
                        y: Math.round(tryCall(d, "getCenterYCoordinate", tryCall(d, "getYCoordinate", 0))),
                        on: tryCall(d, "getPower", true) ? 1 : 0, ports: [] };
            if (opts.ports !== false) {
                eachPort(d, function (p) {
                    var pt = tryCall(p, "getType", -1);
                    var peer = linkPeer(p);
                    var ip = ipOf(p);
                    if (!opts.all && PT_LOGICAL[pt] && !ip) { return; }
                    var row = [p.getName(), ip, peer ? peer.dev : "", peer ? peer.port : "",
                               tryCall(p, "isPortUp", false) ? (tryCall(p, "isProtocolUp", false) ? 2 : 1) : 0, pt];
                    if (opts.detail) {
                        var extra = {};
                        var mac = s(tryCall(p, "getMacAddress", ""));
                        if (mac) { extra.mac = mac; }
                        var desc = s(tryCall(p, "getDescription", ""));
                        if (desc) { extra.desc = desc; }
                        if (typeof p.getAccessVlan === "function") {
                            var mode = tryCall(p, "isAccessPort", true);
                            extra.sw = mode ? ("access " + tryCall(p, "getAccessVlan", 1)) : ("trunk native " + tryCall(p, "getNativeVlanId", 1));
                        }
                        var gw = "";
                        if (typeof p.isDhcpClientOn === "function" && tryCall(p, "isDhcpClientOn", false)) { extra.dhcp = 1; }
                        var bw = tryCall(p, "getBandwidth", 0); if (bw) { extra.bw = bw; }
                        var cr = tryCall(p, "getClockRate", 0); if (cr) { extra.clock = cr; }
                        row.push(extra);
                    }
                    rec.ports.push(row);
                });
            }
            if (opts.gw && HOST_TYPES[rec.t]) {
                // no IPC getter for a host's gateway, read it from the XML
                try {
                    var x = s(d.serializeToXml());
                    var m = x.match(/<PORT_GATEWAY>([^<]+)</) || x.match(/<GATEWAY>([^<]+)</);
                    if (m) { rec.gw = m[1]; }
                    if (/<PORT_DHCP_ENABLE>true</.test(x)) { rec.dhcp = 1; }
                } catch (e) {}
            }
            out.devices.push(rec);
        }
        var lc = net().getLinkCount();
        for (var j = 0; j < lc; j++) {
            try {
                var L = net().getLinkAt(j), a = L.getPort1(), b = L.getPort2();
                out.links.push([a.getOwnerDevice().getName(), a.getName(), b.getOwnerDevice().getName(), b.getName(),
                                CONNECT_NAME[L.getConnectionType()] || s(L.getConnectionType())]);
            } catch (e) {}
        }
        return out;
    };

    P.runningConfig = function (name, startup) {
        var x = s(dev(name).serializeToXml());
        var tag = startup ? "STARTUPCONFIG" : "RUNNINGCONFIG";
        var a = x.indexOf("<" + tag + ">"), b = x.indexOf("</" + tag + ">");
        if (a < 0 || b < 0) { return null; }
        var body = x.substring(a, b), out = [], re = /<LINE>([^<]*)<\/LINE>/g, m;
        while ((m = re.exec(body)) !== null) {
            out.push(m[1].replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&amp;/g, "&"));
        }
        return out.join("\n");
    };

    P.configs = function () {
        var out = {}, n = net().getDeviceCount();
        for (var i = 0; i < n; i++) {
            var d = net().getDeviceAt(i);
            if (!CLI_TYPES[d.getType()]) { continue; }
            try { out[d.getName()] = P.runningConfig(d.getName(), false) || ""; } catch (e) {}
        }
        return out;
    };

    P.hostInfo = function (name) {
        var d = dev(name), r = { n: name };
        try { r.dhcp = d.getDhcpFlag() ? 1 : 0; } catch (e) {}
        eachPort(d, function (p) {
            if (r.port) { return; }
            var ip = ipOf(p);
            if (ip || linkPeer(p)) {
                r.port = p.getName(); r.ip = ip;
                r.mac = s(tryCall(p, "getMacAddress", ""));
            }
        });
        return r;
    };

    P.ports = function (name, freeOnly) {
        var d = dev(name), out = [];
        eachPort(d, function (p) {
            var pt = tryCall(p, "getType", -1);
            if (PT_LOGICAL[pt]) { return; }
            var peer = linkPeer(p);
            if (freeOnly && peer) { return; }
            out.push([p.getName(), pt, peer ? peer.dev + ":" + peer.port : ""]);
        });
        return out;
    };

    P.modules = function (name) {
        var d = dev(name), out = { model: s(d.getModel()), slots: [], supported: [] };
        try { out.supported = d.getSupportedModule() || []; } catch (e) {}
        function walk(m, path) {
            var sc = 0;
            try { sc = m.getSlotCount(); } catch (e) { return; }
            for (var i = 0; i < sc; i++) {
                var child = null;
                try { child = m.getModuleAt(i); } catch (e) {}
                var slotPath = path === "" ? String(i) : path + "/" + i;
                var nm = child ? s(tryCall(child, "getModuleNameAsString", "")) : "";
                out.slots.push([slotPath, s(m.getSlotTypeAt(i)), nm]);
                if (child) { walk(child, slotPath); }
            }
        }
        try { walk(d.getRootModule(), ""); } catch (e) { out.error = String(e); }
        return out;
    };

    // items: [{name, model, types:[int...], x, y}]
    P.addDevices = function (items) {
        var res = [];
        for (var i = 0; i < items.length; i++) {
            var it = items[i], r = { name: it.name };
            try {
                if (net().getDevice(it.name)) { r.error = "exists"; res.push(r); continue; }
                var created = "";
                for (var t = 0; t < it.types.length && !created; t++) {
                    try { created = lw().addDevice(it.types[t], it.model, it.x, it.y); } catch (e) { created = ""; }
                }
                if (!created) { r.error = "PT refused model '" + it.model + "'"; res.push(r); continue; }
                var d = net().getDevice(created);
                if (created !== it.name) { d.setName(it.name); }
                try {
                    if (CLI_TYPES[d.getType()]) {
                        d.skipBoot();
                        setHostname(d);
                        readyLine(d);
                    }
                } catch (e) {}
                r.ok = 1;
            } catch (e) { r.error = String(e); }
            res.push(r);
        }
        return res;
    };

    function portUsable(p, want) {
        if (linkPeer(p)) { return false; }
        var pt = tryCall(p, "getType", -1);
        if (PT_LOGICAL[pt] || pt === 0 || pt === 1) { return want === "console" && pt === 0; }
        if (want === "serial") { return !!PT_SERIAL[pt]; }
        if (want === "fiber") { return !!PT_FIBER[pt]; }
        if (want === "console") { return false; }
        return !!PT_ETH[pt] && !PT_FIBER[pt];
    }

    function speedRank(p) {
        var pt = tryCall(p, "getType", -1);
        return pt === 4 || pt === 6 || pt === 32 ? 0 : (pt === 3 || pt === 5 ? 1 : 2);
    }

    function pickPort(d, want, prefer) {
        var cands = [];
        eachPort(d, function (p, i) { if (portUsable(p, want)) { cands.push([p, i]); } });
        if (!cands.length) { return null; }
        if (prefer === "fast") {
            cands.sort(function (a, b) { return speedRank(a[0]) - speedRank(b[0]) || a[1] - b[1]; });
        } else if (prefer === "access") {
            // keep gig uplinks free
            cands.sort(function (a, b) { return speedRank(b[0]) - speedRank(a[0]) || a[1] - b[1]; });
        }
        return cands[0][0].getName();
    }
    P.pickPort = function (name, want, prefer) { return pickPort(dev(name), want || "eth", prefer || "fast"); };

    // links: [{a, ap, b, bp, cable, prefA, prefB}] ; returns [{a, ap, b, bp, cable, ok|error}]
    P.connect = function (links) {
        var res = [];
        for (var i = 0; i < links.length; i++) {
            var L = links[i], r = { a: L.a, b: L.b, cable: L.cable || "auto" };
            try {
                var da = dev(L.a), db = dev(L.b);
                var want = r.cable === "serial" ? "serial" : (r.cable === "fiber" ? "fiber" : (r.cable === "console" ? "console" : "eth"));
                r.ap = L.ap || pickPort(da, want, L.prefA || "fast");
                r.bp = L.bp || pickPort(db, want, L.prefB || "fast");
                if (want === "eth" && r.cable === "auto" && !(L.ap && L.bp) && (!r.ap || !r.bp)) {
                    // out of copper (Router-PT Fa4/0 and Fa5/0 are fiber), try fiber
                    var fa = L.ap || pickPort(da, "fiber", "fast"), fb = L.bp || pickPort(db, "fiber", "fast");
                    if (fa && fb && PT_FIBER[tryCall(da.getPort(fa), "getType", -1)] && PT_FIBER[tryCall(db.getPort(fb), "getType", -1)]) {
                        r.ap = fa; r.bp = fb; r.cable = "fiber";
                    }
                }
                if (!r.ap) { throw new Error(L.a + " has no free " + want + " port (add a module?)"); }
                if (!r.bp) { throw new Error(L.b + " has no free " + want + " port (add a module?)"); }
                if (!da.getPort(r.ap)) { throw new Error(L.a + " has no port " + r.ap); }
                if (!db.getPort(r.bp)) { throw new Error(L.b + " has no port " + r.bp); }
                if (r.cable === "auto") {
                    var pa = tryCall(da.getPort(r.ap), "getType", -1), pb = tryCall(db.getPort(r.bp), "getType", -1);
                    if (PT_SERIAL[pa] && PT_SERIAL[pb]) { r.cable = "serial"; }
                    else if (PT_FIBER[pa] && PT_FIBER[pb]) { r.cable = "fiber"; }
                    else if (PT_ETH[pa] && PT_ETH[pb]) {
                        // PT's auto cable can pick straight between two routers, so pick by MDI class
                        r.cable = !MDIX_TYPES[da.getType()] === !MDIX_TYPES[db.getType()] ? "cross" : "straight";
                    }
                }
                var type = CONNECT[r.cable];
                if (type === undefined) { throw new Error("unknown cable " + r.cable); }
                var ok = lw().createLink(L.a, r.ap, L.b, r.bp, type);
                if (!ok && (!L.cable || L.cable === "auto")) {
                    // try the other cable types
                    var alt = ["straight", "cross", "auto"];
                    for (var j = 0; j < alt.length && !ok; j++) {
                        if (alt[j] !== r.cable && lw().createLink(L.a, r.ap, L.b, r.bp, CONNECT[alt[j]])) { ok = true; r.cable = alt[j]; }
                    }
                }
                if (!ok) { throw new Error("PT refused link " + L.a + ":" + r.ap + " <-> " + L.b + ":" + r.bp + " (" + r.cable + ")"); }
                r.ok = 1;
            } catch (e) { r.error = String(e.message || e); }
            res.push(r);
        }
        return res;
    };

    P.unlink = function (items) {
        var res = [];
        for (var i = 0; i < items.length; i++) {
            try { res.push(lw().deleteLink(items[i].device, items[i].port) ? 1 : 0); }
            catch (e) { res.push(String(e)); }
        }
        return res;
    };

    P.remove = function (names) {
        var res = {};
        for (var i = 0; i < names.length; i++) {
            try { res[names[i]] = lw().removeDevice(names[i]) ? 1 : 0; } catch (e) { res[names[i]] = String(e); }
        }
        return res;
    };

    P.clearAll = function () {
        var names = [], n = net().getDeviceCount();
        for (var i = 0; i < n; i++) { names.push(net().getDeviceAt(i).getName()); }
        return P.remove(names);
    };

    P.move = function (items) {
        for (var i = 0; i < items.length; i++) {
            var d = dev(items[i].name);
            if (typeof d.moveToLocationCentered === "function") { d.moveToLocationCentered(items[i].x, items[i].y); }
            else { d.moveToLocation(items[i].x, items[i].y); }
        }
        return items.length;
    };

    P.rename = function (oldName, newName) { dev(oldName).setName(newName); return 1; };

    P.power = function (names, on) {
        for (var i = 0; i < names.length; i++) {
            var d = dev(names[i]);
            d.setPower(on);
            if (on) { try { if (CLI_TYPES[d.getType()]) { d.skipBoot(); } } catch (e) {} }
        }
        return names.length;
    };

    // hostname = device name
    function setHostname(d) {
        var hn = s(d.getName()).replace(/[^A-Za-z0-9_-]/g, "-").replace(/^[^A-Za-z]+/, "");
        if (hn) { d.enterCommand("hostname " + hn, "global"); d.enterCommand("end", ""); }
    }

    // modules need the power off, which loses the running config, so save it first
    P.addModule = function (name, slot, model, moduleType) {
        var d = dev(name), was = d.getPower(), cli = !!CLI_TYPES[d.getType()];
        if (was && cli) {
            try { readyLine(d); setHostname(d); d.enterCommand("write memory", "enable"); } catch (e) {}
        }
        if (was) { d.setPower(false); }
        var ok = false, err = "";
        try { ok = d.addModule(String(slot), moduleType, model); } catch (e) { err = String(e); }
        if (was) {
            d.setPower(true);
            if (cli) { try { d.skipBoot(); readyLine(d); } catch (e) {} }
        }
        if (!ok) { throw new Error("addModule(" + slot + ", " + model + ") failed " + err + ", check the slot with pt_device_modules"); }
        return P.ports(name, false);
    };

    P.hostConfig = function (name, c) {
        var d = dev(name), port = c.port ? d.getPort(c.port) : null;
        if (!port) {
            eachPort(d, function (p) {
                if (!port && typeof p.setIpSubnetMask === "function" && PT_ETH[tryCall(p, "getType", -1)]) { port = p; }
            });
        }
        if (!port) { throw new Error(name + ": no configurable ethernet port"); }
        if (c.dhcp) {
            try { d.setDhcpFlag(true); } catch (e) { port.setDhcpClientFlag(true); }
        } else {
            try { d.setDhcpFlag(false); } catch (e) {}
            try { port.setDhcpClientFlag(false); } catch (e) {}
            if (c.ip) { port.setIpSubnetMask(c.ip, c.mask); }
            if (c.gw) { try { d.setDefaultGateway(c.gw); } catch (e) { port.setDefaultGateway(c.gw); } }
            if (c.dns) { port.setDnsServerIp(c.dns); }
        }
        if (c.ipv6) {
            try { port.setIpv6Enabled(true); port.addIpv6Address(c.ipv6.split("/")[0], parseInt(c.ipv6.split("/")[1] || "64", 10), 0, false); } catch (e) {}
        }
        if (c.gw6) { try { port.setv6DefaultGateway(c.gw6); } catch (e) {} }
        return port.getName();
    };

    function firstString(args) {
        if (args === null || args === undefined) { return ""; }
        if (typeof args === "string") { return args; }
        for (var k in args) { if (typeof args[k] === "string") { return args[k]; } }
        return "";
    }

    function statusOf(ret) {
        if (ret === null || ret === undefined) { return [0, ""]; }
        if (ret.length !== undefined && typeof ret !== "string") { return [Number(ret[0]) || 0, s(ret[1])]; }
        if (typeof ret === "object") {
            if ("first" in ret) { return [Number(ret.first) || 0, s(ret.second)]; }
        }
        return [Number(ret) || 0, ""];
    }

    var STATUS = ["ok", "ambiguous", "invalid", "incomplete", "not implemented"];

    function lineOf(d) {
        try { return d.getCommandLine(); } catch (e) { return null; }
    }

    // mode: config | exec | raw
    P.cli = function (name, cmds, mode) {
        var d = dev(name);
        try { if (d.isBooting()) { d.skipBoot(); } } catch (e) {}
        if (typeof d.enterCommand !== "function") { throw new Error(name + " has no IOS CLI (use pt_configure_host for end devices)"); }
        var line = lineOf(d), buf = [], handler = function (src, args) { buf.push(firstString(args)); };
        var hooked = false;
        if (line) { try { line.registerEvent("outputWritten", null, handler); hooked = true; } catch (e) {} }
        var results = [];
        try {
            if (mode === "exec") { d.enterCommand("terminal length 0", "enable"); }
            var recover = false;
            for (var i = 0; i < cmds.length; i++) {
                var c = cmds[i];
                var m = "";
                // after an error, start the next top-level line from global config
                if (mode === "config" && (i === 0 || (recover && !/^\s/.test(c)))) { m = "global"; recover = false; }
                if (mode === "exec") { m = "enable"; }
                var start = buf.length;
                var st = statusOf(d.enterCommand(c, m));
                // the IPC CLI doesn't fall back to the parent mode like IOS, retry from global
                if (mode === "config" && st[0] === 2 && m !== "global") {
                    var st2 = statusOf(d.enterCommand(c, "global"));
                    if (st2[0] === 0) { st = st2; buf.length = start; }
                }
                var out = buf.slice(start).join("");
                if (!out && st[1]) { out = st[1]; }
                if (st[0] !== 0 || /^\s*% (Invalid|Incomplete|Ambiguous|Interface range)/m.test(out)) { recover = true; }
                results.push([c, st[0], out]);
            }
            if (mode === "config") { d.enterCommand("end", ""); }
        } finally {
            if (hooked) { try { line.unregisterEvent("outputWritten", null, handler); } catch (e) {} }
        }
        var prompt = "";
        try { prompt = line ? s(line.getPrompt()) : ""; } catch (e) {}
        return { r: results, prompt: prompt, hooked: hooked ? 1 : 0, statusNames: STATUS };
    };

    P._jobs = P._jobs || {};
    P._seq = P._seq || 0;

    // answer the setup dialog and leave config mode
    function readyLine(d) {
        var line = lineOf(d);
        if (!line) { return null; }
        try { if (d.isBooting()) { d.skipBoot(); } } catch (e) {}
        for (var i = 0; i < 3; i++) {
            var p = s(tryCall(line, "getPrompt", ""));
            if (/yes\/no/i.test(p)) { line.enterCommand("no"); line.enterCommand(""); }
            else if (/\[confirm\]|RETURN/i.test(p) || p === "") { line.enterCommand(""); }
            else if (/\(config[^)]*\)#\s*$/.test(p)) { line.enterCommand("end"); }
            else { break; }
        }
        return line;
    }
    P.readyLine = function (name) { var l = readyLine(dev(name)); return l ? s(l.getPrompt()) : ""; };

    // ping/traceroute return right away from enterCommand, so run them on the console
    // line (or a host's command prompt) and collect outputWritten events
    P.startCmd = function (name, cmd) {
        var d = dev(name), line = null;
        try { if (typeof d.getCommandPrompt === "function") { line = d.getCommandPrompt(); } } catch (e) {}
        if (!line) { line = readyLine(d); }
        if (!line) { throw new Error(name + " has no command line"); }
        var id = "j" + (++P._seq);
        var job = { buf: [], line: line, started: new Date().getTime() };
        job.h = function (src, args) { job.buf.push(firstString(args)); };
        line.registerEvent("outputWritten", null, job.h);
        P._jobs[id] = job;
        line.enterCommand(cmd);
        return id;
    };

    P.readCmds = function (ids, stop) {
        var out = {};
        for (var i = 0; i < ids.length; i++) {
            var j = P._jobs[ids[i]];
            if (!j) { out[ids[i]] = null; continue; }
            out[ids[i]] = j.buf.join("");
            if (stop) {
                try { j.line.unregisterEvent("outputWritten", null, j.h); } catch (e) {}
                delete P._jobs[ids[i]];
            }
        }
        return out;
    };

    P.info = function () {
        var aw = ipc.appWindow(), r = {};
        r.version = s(tryCall(aw, "getVersion", ""));
        try { r.file = s(aw.getActiveFile().getSavedFilename ? aw.getActiveFile().getSavedFilename() : ""); } catch (e) {}
        r.devices = 0;
        for (var i = 0; i < net().getDeviceCount(); i++) { if (!HIDDEN_TYPES[net().getDeviceAt(i).getType()]) { r.devices++; } }
        r.links = net().getLinkCount();
        r.sim = tryCall(aw, "isSimulationMode", false) ? 1 : 0;
        r.userFolder = s(tryCall(aw, "getUserFolder", ""));
        return r;
    };

    P.fileNew = function () { return ipc.appWindow().fileNew(false) ? 1 : 0; };
    P.fileOpen = function (path) { return s(ipc.appWindow().fileOpen(path)); };
    P.fileSave = function (path) {
        var aw = ipc.appWindow();
        if (path) { aw.fileSaveAsNoPrompt(path, true); return path; }
        return aw.fileSave() ? 1 : 0;
    };

    var B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    function b64(bytes) {
        var out = [], i, n = bytes.length;
        for (i = 0; i + 2 < n; i += 3) {
            var v = ((bytes[i] & 255) << 16) | ((bytes[i + 1] & 255) << 8) | (bytes[i + 2] & 255);
            out.push(B64[v >> 18 & 63] + B64[v >> 12 & 63] + B64[v >> 6 & 63] + B64[v & 63]);
        }
        if (i < n) {
            var w = (bytes[i] & 255) << 16 | (i + 1 < n ? (bytes[i + 1] & 255) << 8 : 0);
            out.push(B64[w >> 18 & 63] + B64[w >> 12 & 63] + (i + 1 < n ? B64[w >> 6 & 63] : "=") + "=");
        }
        return out.join("");
    }
    P.image = function (fmt) {
        var bytes = lw().getWorkspaceImage(fmt || "PNG");
        if (typeof bytes === "string") { return bytes; }
        return b64(bytes);
    };

    P.note = function (x, y, text) { return s(lw().addNote(x, y, 0, text)); };
    P.fastForward = function (times) {
        var tb = ipc.appWindow().getRealtimeToolbar();
        for (var i = 0; i < (times || 1); i++) { tb.fastForwardTime(); }
        return 1;
    };

    P.cmdLog = function (limit, device) {
        var L = ipc.commandLog(), n = L.getEntryCount(), out = [];
        for (var i = n - 1; i >= 0 && out.length < limit; i--) {
            var e = L.getEntryAt(i);
            if (device && e.getDeviceName() !== device) { continue; }
            out.push([s(e.getTimeToString()), s(e.getDeviceName()), s(e.getPrompt()), s(e.getCommand())]);
        }
        return out.reverse();
    };

    var TRAFFIC = { 0: "ICMP", 1: "TCP", 2: "UDP", 3: "RIPv1", 4: "RIPv2", 5: "ARP", 6: "CDP", 7: "DHCP", 11: "STP",
                    12: "OSPF", 13: "DTP", 17: "HTTP", 18: "HTTPS", 19: "DNS", 36: "BGP" };

    P.sim = function (action, arg) {
        var sim = ipc.simulation(), aw = ipc.appWindow();
        if (action === "mode") { sim.setSimulationMode(!!arg); }
        else if (action === "reset") { sim.resetSimulation(); }
        else if (action === "forward") { for (var i = 0; i < (arg || 1); i++) { sim.forward(); } }
        else if (action === "back") { for (var j = 0; j < (arg || 1); j++) { sim.backward(); } }
        else if (action === "pdu") { return s(aw.getUserCreatedPDU().addSimplePdu(arg[0], arg[1])); }
        else if (action === "filter") { var sp = aw.getSimulationPanel(); sp.setAllFilters(); }
        return { sim: sim.isSimulationMode() ? 1 : 0, t: tryCall(sim, "getCurrentSimTime", 0),
                 frames: tryCall(sim, "getFrameInstanceCount", 0) };
    };

    P.frames = function (limit) {
        var sim = ipc.simulation(), n = sim.getFrameInstanceCount(), out = [];
        for (var i = Math.max(0, n - limit); i < n; i++) {
            var f = sim.getFrameInstanceAt(i), st = "?";
            try {
                if (f.isFrameAccepted()) { st = "accepted"; } else if (f.isFrameDropped()) { st = "dropped"; }
                else if (f.isFrameBuffered()) { st = "buffered"; } else if (f.isFrameOnTransit()) { st = "transit"; }
                else if (f.isFrameSent()) { st = "sent"; }
            } catch (e) {}
            var ty = s(tryCall(f, "getUserTrafficType", ""));
            out.push([i, s(tryCall(f, "getSourceString", "")), s(tryCall(f, "getDestinationString", "")), TRAFFIC[ty] || ty, st]);
        }
        return out;
    };
})(__g.PTM);
