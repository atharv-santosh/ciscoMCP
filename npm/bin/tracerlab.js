#!/usr/bin/env node
// Thin launcher: runs the Python server through uvx so node users get one command.
const { spawn, spawnSync } = require("node:child_process");

const PKG = "tracerlab";
const ENTRY = "packet-tracer-mcp";
const args = process.argv.slice(2);

if (spawnSync("uvx", ["--version"], { stdio: "ignore" }).error) {
  console.error("tracerlab needs uv. Install it from https://docs.astral.sh/uv/ then run this again.");
  process.exit(1);
}

const child = spawn("uvx", ["--from", PKG, ENTRY, ...args], { stdio: "inherit" });
child.on("exit", (code, signal) => (signal ? process.kill(process.pid, signal) : process.exit(code ?? 0)));
