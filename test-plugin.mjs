#!/usr/bin/env node
// Quick local MCP test for the x402 warranty plugin.
// Usage: WARRANTY_BASE_URL=https://x402-warranty-server.onrender.com node test-plugin.mjs

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const pluginPath = join(__dirname, "paybox-plugin.mjs");

const child = spawn("node", [pluginPath], {
  env: {
    ...process.env,
    WARRANTY_BASE_URL: process.env.WARRANTY_BASE_URL || "https://x402-warranty-server.onrender.com",
  },
  stdio: ["pipe", "pipe", "inherit"],
});

let buffer = "";

function send(req) {
  child.stdin.write(JSON.stringify(req) + "\n");
}

child.stdout.on("data", (chunk) => {
  buffer += chunk.toString("utf8");
  const lines = buffer.split("\n");
  buffer = lines.pop();
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const msg = JSON.parse(line);
      console.log("<--", JSON.stringify(msg, null, 2));
    } catch {
      console.log("<--", line);
    }
  }
});

child.on("error", (err) => {
  console.error("Plugin error:", err);
  process.exit(1);
});

function meta() {
  return {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": { name: "test-client", version: "0.1.0" },
    "io.modelcontextprotocol/clientCapabilities": {},
  };
}

setTimeout(() => {
  send({
    jsonrpc: "2.0",
    id: 1,
    method: "tools/list",
    params: { _meta: meta() },
  });

  setTimeout(() => {
    send({
      jsonrpc: "2.0",
      id: 2,
      method: "tools/call",
      params: {
        _meta: meta(),
        name: "quote_warranty",
        arguments: {
          url: "https://example.com/api/data",
          method: "GET",
        },
      },
    });

    setTimeout(() => {
      child.kill();
      process.exit(0);
    }, 4000);
  }, 1000);
}, 500);
