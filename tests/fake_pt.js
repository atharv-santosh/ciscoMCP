// Fake PT for tests: the parts of the IPC API that runtime.js uses, plus the web view loop.
// node tests/fake_pt.js <port>
"use strict";
const http = require("http");
const PORT = parseInt(process.argv[2] || "39150", 10);

const MODELS = {
  "2911": { type: 0, ports: [["GigabitEthernet0/0", 4], ["GigabitEthernet0/1", 4], ["GigabitEthernet0/2", 4]] },
  "Router-PT": { type: 0, ports: [["FastEthernet0/0", 3], ["FastEthernet1/0", 3], ["Serial2/0", 7], ["Serial3/0", 7], ["FastEthernet4/0", 3], ["FastEthernet5/0", 3]] },
  "2960-24TT": { type: 1, ports: [...Array.from({ length: 24 }, (_, i) => [`FastEthernet0/${i + 1}`, 3]), ["GigabitEthernet0/1", 4], ["GigabitEthernet0/2", 4]] },
  "3560-24PS": { type: 16, ports: [...Array.from({ length: 24 }, (_, i) => [`FastEthernet0/${i + 1}`, 3]), ["GigabitEthernet0/1", 4], ["GigabitEthernet0/2", 4]] },
  "PC-PT": { type: 8, ports: [["FastEthernet0", 3]] },
  "Server-PT": { type: 9, ports: [["FastEthernet0", 3]] },
};

const devices = []; // ordered
let links = [];
const lines = []; // executed CLI lines, for assertions

class Port {
  constructor(dev, name, type) { Object.assign(this, { dev, name, type, ip: "0.0.0.0", mask: "0.0.0.0", up: true, link: null, gw: "", dhcp: false, mac: "0000.0000.00" + (devices.length + 10) }); }
  getName() { return this.name; }
  getType() { return this.type; }
  getOwnerDevice() { return this.dev; }
  getLink() { return this.link; }
  isPortUp() { return !!this.link && this.up; }
  isProtocolUp() { return !!this.link && this.up; }
  getMacAddress() { return this.mac; }
  getDescription() { return ""; }
  getBandwidth() { return 100000; }
  getClockRate() { return 0; }
  getIpAddress() { return this.ip; }
  getSubnetMask() { return this.mask; }
  setIpSubnetMask(ip, mask) { this.ip = ip; this.mask = mask; }
  setDefaultGateway(gw) { this.gw = gw; }
  setDnsServerIp() {}
  setDhcpClientFlag(f) { this.dhcp = f; }
  isDhcpClientOn() { return this.dhcp; }
}

class TerminalLine {
  constructor(dev) { this.dev = dev; this.handlers = []; }
  registerEvent(ev, obj, fn) { this.handlers.push(fn); }
  unregisterEvent(ev, obj, fn) { this.handlers = this.handlers.filter(h => h !== fn); }
  emit(text) { this.handlers.forEach(h => h({ className: "TerminalLine" }, { msg: text, flag: false, n: 0 })); }
  getPrompt() { return this.dev.name + "#"; }
  enterCommand(cmd) {
    // PC command prompt: async ping output
    const m = cmd.match(/^ping -n (\d+) (\S+)/);
    if (m) {
      const n = +m[1];
      setTimeout(() => {
        this.emit(`\nPinging ${m[2]} with 32 bytes of data:\n\n`);
        for (let i = 0; i < n; i++) this.emit(`Reply from ${m[2]}: bytes=32 time<1ms TTL=128\n`);
        this.emit(`\nPing statistics for ${m[2]}:\n    Packets: Sent = ${n}, Received = ${n}, Lost = 0 (0% loss),\nApproximate round trip times in milli-seconds:\n    Minimum = 0ms, Maximum = 0ms, Average = 0ms\n`);
      }, 150);
    }
  }
}

class Device {
  constructor(name, model, x, y) {
    const spec = MODELS[model];
    Object.assign(this, { name, model, type: spec.type, x, y, power: true, booting: true });
    this.ports = spec.ports.map(([n, t]) => new Port(this, n, t));
    this.line = new TerminalLine(this);
  }
  getName() { return this.name; }
  setName(n) { this.name = n; }
  getModel() { return this.model; }
  getType() { return this.type; }
  getPower() { return this.power; }
  setPower(p) { this.power = p; }
  getCenterXCoordinate() { return this.x; }
  getCenterYCoordinate() { return this.y; }
  moveToLocationCentered(x, y) { this.x = x; this.y = y; return true; }
  getPortCount() { return this.ports.length; }
  getPortAt(i) { return this.ports[i]; }
  getPort(n) { return this.ports.find(p => p.name === n) || null; }
  isBooting() { return this.booting; }
  skipBoot() { this.booting = false; }
  getCommandLine() { return this.line; }
  getCommandPrompt() { return this.type >= 8 ? this.line : null; }
  setDhcpFlag(f) { this.ports[0].dhcp = f; }
  setDefaultGateway(gw) { this.ports[0].gw = gw; }
  serializeToXml() {
    const p = this.ports[0];
    return `<ENGINE><PORT_GATEWAY>${p.gw}</PORT_GATEWAY><PORT_DHCP_ENABLE>${p.dhcp}</PORT_DHCP_ENABLE>` +
      `<RUNNINGCONFIG><LINE>hostname ${this.name}</LINE><LINE>interface GigabitEthernet0/0</LINE><LINE> ip address 1.1.1.1 255.0.0.0</LINE></RUNNINGCONFIG></ENGINE>`;
  }
  enterCommand(cmd, mode) {
    if (this.type >= 8) throw new Error("no CLI");
    lines.push([this.name, cmd, mode]);
    const m = cmd.match(/^\s*ip address (\S+) (\S+)/);
    if (m && this._iface) { const p = this.getPort(this._iface); if (p) p.setIpSubnetMask(m[1], m[2]); }
    const i = cmd.match(/^interface (\S+)/);
    if (i) this._iface = i[1];
    if (/^\s*exit|^end/.test(cmd)) this._iface = null;
    if (/^show version/.test(cmd)) { this.line.emit("Cisco IOS Software, fake\n"); return [0, ""]; }
    if (/bogus/.test(cmd)) { this.line.emit("\n% Invalid input detected at '^' marker.\n"); return [2, ""]; }
    return [0, ""];
  }
  getRootModule() { return { getSlotCount: () => 0 }; }
  getSupportedModule() { return []; }
}

class Link {
  constructor(a, b, type) { Object.assign(this, { a, b, type }); }
  getPort1() { return this.a; }
  getPort2() { return this.b; }
  getConnectionType() { return this.type; }
}

const logical = {
  addDevice(type, model, x, y) {
    if (!MODELS[model] || MODELS[model].type !== type) return "";
    const base = model.replace(/-.*/, "");
    let n = 0;
    while (devices.some(d => d.name === `${base}${n}`)) n++;
    devices.push(new Device(`${base}${n}`, model, x, y));
    return `${base}${n}`;
  },
  removeDevice(name) {
    const d = devices.find(x => x.name === name);
    if (!d) return false;
    links = links.filter(l => { const keep = l.a.dev !== d && l.b.dev !== d; if (!keep) { l.a.link = null; l.b.link = null; } return keep; });
    devices.splice(devices.indexOf(d), 1);
    return true;
  },
  createLink(a, ap, b, bp, type) {
    const pa = devices.find(d => d.name === a)?.getPort(ap), pb = devices.find(d => d.name === b)?.getPort(bp);
    if (!pa || !pb || pa.link || pb.link) return false;
    const l = new Link(pa, pb, type);
    pa.link = l; pb.link = l; links.push(l);
    return true;
  },
  deleteLink(dev, port) {
    const p = devices.find(d => d.name === dev)?.getPort(port);
    if (!p || !p.link) return false;
    const l = p.link; l.a.link = null; l.b.link = null; links = links.filter(x => x !== l);
    return true;
  },
  addNote() { return "{note}"; },
  getWorkspaceImage() { return [137, 80, 78, 71]; },
};

global.ipc = {
  network: () => ({
    getDevice: n => devices.find(d => d.name === n) || null,
    getDeviceCount: () => devices.length,
    getDeviceAt: i => devices[i],
    getLinkCount: () => links.length,
    getLinkAt: i => links[i],
  }),
  appWindow: () => ({
    getVersion: () => "9.0.0.0810-fake",
    getActiveWorkspace: () => ({ getLogicalWorkspace: () => logical }),
    isSimulationMode: () => false,
    getUserFolder: () => "/tmp",
    getActiveFile: () => ({ getSavedFilename: () => "" }),
    getRealtimeToolbar: () => ({ fastForwardTime() {} }),
  }),
  commandLog: () => ({ getEntryCount: () => 0 }),
};
global.__fakeLines = lines;

function ptmcpExec(code) {
  try {
    const v = new Function(code)();
    return JSON.stringify({ ok: true, value: v === undefined ? null : v });
  } catch (e) {
    return JSON.stringify({ ok: false, error: String(e && e.message ? e.message : e) });
  }
}

function request(method, path, body) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: "127.0.0.1", port: PORT, method, path, headers: { "Content-Type": "text/plain" } }, res => {
      let d = ""; res.on("data", c => (d += c)); res.on("end", () => resolve(d));
    });
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}

(async function loop() {
  for (;;) {
    try {
      const job = JSON.parse(await request("GET", "/poll?pt=9.0.0-fake&s=1"));
      if (job.id) {
        const env = JSON.parse(ptmcpExec(job.code));
        env.id = job.id;
        await request("POST", "/result", JSON.stringify(env));
      }
    } catch (e) {
      await new Promise(r => setTimeout(r, 200));
    }
  }
})();
