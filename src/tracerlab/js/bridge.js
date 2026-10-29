// Web view side of the bridge. PT's web view can't read cross-origin responses,
// so jobs come in as JSONP and results go out as chunked GETs.
(function () {
  if (window.ptmcpLoaded) { return; }
  window.ptmcpLoaded = true;
  var BASE = "http://127.0.0.1:__PORT__";
  var BUILD = "__BUILD__";  // server reloads us when its bridge.js changes
  var SES = String(Date.now());
  var n = 0, connected = false, fails = 0, seq = 0;

  function st(t, c) { var e = document.getElementById("st"); if (e) { e.textContent = t; e.className = "pill " + c; } }
  function lg(t) { var e = document.getElementById("log"); if (e) { e.textContent = new Date().toTimeString().slice(0, 8) + " " + t + "\n" + e.textContent.slice(0, 3000); } }
  function beacon(path) { var i = new Image(); i.src = BASE + path + (path.indexOf("?") < 0 ? "?" : "&") + "t=" + Date.now(); }
  function getScript(url) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.onload = function () { s.remove(); resolve(); };
      s.onerror = function () { s.remove(); reject(new Error("GET failed")); };
      s.src = url;
      document.body.appendChild(s);
    });
  }
  var CHUNK = 6000;  // chars per GET
  async function send(id, body) {
    var parts = Math.max(1, Math.ceil(body.length / CHUNK));
    for (var i = 0; i < parts; i++) {
      var url = BASE + "/r?id=" + id + "&i=" + i + "&n=" + parts + "&d=" + encodeURIComponent(body.substr(i * CHUNK, CHUNK));
      for (var a = 0; ; a++) {
        try { await getScript(url); break; } catch (e) { if (a >= 3) { lg("result " + id + " lost: " + e); return; } await sleep(200); }
      }
    }
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  async function engineReady() {
    for (var i = 0; i < 100; i++) {
      if (typeof $seev === "function" || typeof $se === "function") { return true; }
      await sleep(100);
    }
    return false;
  }

  async function evalInEngine(code) {
    if (typeof $seev === "function") { return await $seev("ptmcpExec(" + JSON.stringify(code) + ")"); }
    var r = $se("ptmcpExec", code);
    if (r && typeof r.then === "function") { r = await r; }
    return r;
  }

  function jsonp() {
    return new Promise(function (resolve, reject) {
      var cb = "ptmcpCb" + (++seq);
      var s = document.createElement("script");
      var done = function () { delete window[cb]; s.remove(); };
      window[cb] = function (job) { done(); resolve(job); };
      s.onerror = function () { done(); reject(new Error("poll failed")); };
      s.src = BASE + "/poll?cb=" + cb + "&s=" + SES + "&pt=9";
      document.body.appendChild(s);
    });
  }

  async function handle(job) {
    var out;
    try { out = await evalInEngine(job.code); } catch (e) { out = JSON.stringify({ ok: false, error: "bridge: " + e }); }
    if (typeof out !== "string") { out = JSON.stringify({ ok: false, error: "bridge: engine returned " + (typeof out) + " " + String(out) }); }
    var env;
    try { env = JSON.parse(out); } catch (e) { env = { ok: false, error: "bridge: bad JSON from engine" }; }
    env.id = job.id;
    n++;
    var ne = document.getElementById("n"); if (ne) { ne.textContent = n; }
    if (!env.ok) { lg("job " + job.id + ": " + env.error); }
    await send(job.id, JSON.stringify(env));
  }

  async function loop() {
    var ready = await engineReady();
    beacon("/hello?se=" + (typeof $se) + "&seev=" + (typeof $seev) + "&ready=" + ready);
    if (!ready) { st("PT scripting API missing", "off"); lg("$se/$seev not injected"); }
    for (;;) {
      try {
        var job = await jsonp();
        if (!connected) { connected = true; st("connected", "on"); lg("connected"); }
        fails = 0;
        if (job && job.build && job.build !== BUILD) { lg("bridge update, reloading"); location.reload(); return; }
        if (job && job.id) { await handle(job); }
      } catch (e) {
        fails++;
        if (connected || fails === 1) { connected = false; st("waiting for MCP server", "off"); lg(String(e)); }
        await sleep(Math.min(5000, 500 * fails));
      }
    }
  }
  loop();
})();
