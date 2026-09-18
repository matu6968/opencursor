#!/usr/bin/env node
/**
 * Capture official @cursor/sdk local-agent HTTP/1.1 RPC paths, headers,
 * and the first BidiAppend payload (decoded later in Python).
 * Does not log Authorization tokens.
 */
import https from "node:https";
import http from "node:http";
import http2 from "node:http2";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const envFile = path.join(root, ".env");
if (fs.existsSync(envFile)) {
  for (const line of fs.readFileSync(envFile, "utf8").split("\n")) {
    const m = line.match(/^([A-Z0-9_]+)=(.*)$/);
    if (m && !process.env[m[1]]) {
      process.env[m[1]] = m[2].replace(/^["']|["']$/g, "");
    }
  }
}

const dumpDir = path.join(root, "opencursor", ".capture");
fs.mkdirSync(dumpDir, { recursive: true });
let appendCount = 0;

function patch(mod, label) {
  const orig = mod.request;
  mod.request = function patchedRequest(options, callback) {
    const opts = typeof options === "string" || options instanceof URL ? new URL(options) : options;
    const host = opts.hostname || opts.host || "";
    const urlPath = opts.path || opts.pathname || "";
    const headers = { ...(opts.headers || {}) };
    const redacted = { ...headers };
    if (redacted.authorization || redacted.Authorization) {
      redacted.authorization = "Bearer <redacted>";
      delete redacted.Authorization;
    }
    console.error(`[${label}] ${opts.method || "GET"} https://${host}${urlPath}`);
    console.error(`[${label}] headers ${JSON.stringify(redacted)}`);
    const req = orig.call(this, options, callback);
    if (String(urlPath).includes("BidiAppend")) {
      const chunks = [];
      const origWrite = req.write.bind(req);
      const origEnd = req.end.bind(req);
      req.write = function (chunk, encoding, cb) {
        if (chunk) chunks.push(Buffer.from(chunk, encoding));
        return origWrite(chunk, encoding, cb);
      };
      req.end = function (chunk, encoding, cb) {
        if (chunk && typeof chunk !== "function") chunks.push(Buffer.from(chunk, encoding));
        const body = Buffer.concat(chunks);
        appendCount += 1;
        const out = path.join(dumpDir, `bidi_append_${appendCount}.bin`);
        fs.writeFileSync(out, body);
        console.error(`[${label}] saved BidiAppend body ${body.length} bytes -> ${out}`);
        return origEnd(chunk, encoding, cb);
      };
    }
    return req;
  };
}

patch(https, "https");
patch(http, "http");

const origConnect = http2.connect;
http2.connect = function patchedConnect(...args) {
  const authority = String(args[0] || "");
  console.error(`[http2] connect ${authority}`);
  const session = origConnect.apply(this, args);
  const origRequest = session.request.bind(session);
  session.request = function patchedRequest(headers, options) {
    const p = headers && (headers[":path"] || headers[":PATH"]);
    const m = headers && (headers[":method"] || headers[":METHOD"] || "POST");
    console.error(`[http2] ${m} ${authority}${p || ""}`);
    return origRequest(headers, options);
  };
  return session;
};

const { Agent, configureCursorSdk } = await import("@cursor/sdk");
configureCursorSdk({ local: { useHttp1ForAgent: true } });

if (!process.env.CURSOR_API_KEY) {
  console.error("no CURSOR_API_KEY; skip live capture");
  process.exit(2);
}

const cwd = process.argv[2] || process.cwd();
const agent = await Agent.create({
  apiKey: process.env.CURSOR_API_KEY,
  model: { id: "composer-2.5" },
  local: { cwd, sandboxOptions: { enabled: false } },
  tools: [],
});
try {
  const run = await agent.send("Reply with the single word pong and nothing else.");
  for await (const event of run.stream()) {
    if (event.type === "assistant" || event.type === "status") {
      console.error(`[event] ${event.type} ${JSON.stringify(event).slice(0, 200)}`);
    }
  }
  const result = await run.wait();
  console.error(`[done] ${result.status}`);
} finally {
  await agent[Symbol.asyncDispose]?.();
}
