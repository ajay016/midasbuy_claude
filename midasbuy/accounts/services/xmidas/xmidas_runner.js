/*
 * Headless host for the Midasbuy "Chaos VM" (window.xMidas) — runs the REAL
 * encryption outside a browser.
 *
 * window.xMidas is the production whitebox cipher that turns a JSON payload into
 * the `encrypt_msg` field. It only needs a tiny DOM shim: two hidden inputs
 * (xMidasToken / xMidasVersion) plus atob/btoa. We load the VM, prime it once,
 * then encrypt.
 *
 * Protocol: read one JSON line from stdin:
 *     {"token": "<hex>", "version": "1.0.1", "payload": {<full payload obj>}}
 * Write one JSON line to stdout:
 *     {"ok": true, "encrypt_msg": "<base64>", "ctoken": "<hex>", "ctoken_ver": "1.0.1"}
 *   or {"ok": false, "error": "..."}
 *
 * The encrypt_msg construction mirrors commonSdkApi.js exactly:
 *     hex         = window.xMidas({d: JSON.stringify(payload)})
 *     encrypt_msg = btoa(String.fromCharCode(...hexToBytes(hex)))
 *                 = Buffer.from(hex, "hex").toString("base64")
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function buildWindow(token, version) {
  const inputs = {
    xMidasToken: { value: token, id: "xMidasToken" },
    xMidasVersion: { value: version || "1.0.1", id: "xMidasVersion" },
  };
  const win = {};
  win.window = win;
  win.self = win;
  win.navigator = {
    userAgent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    language: "en-US",
    languages: ["en-US", "en"],
    platform: "Win32",
    appName: "Netscape",
  };
  win.location = {
    href: "https://www.midasbuy.com/midasbuy/bd/redeem/pubgm",
    host: "www.midasbuy.com",
    hostname: "www.midasbuy.com",
    protocol: "https:",
    origin: "https://www.midasbuy.com",
  };
  win.document = {
    getElementById: (id) => inputs[id] || null,
    createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
    getElementsByTagName: () => [],
    body: { appendChild() {} },
    head: { appendChild() {} },
    documentElement: { style: {} },
    addEventListener() {},
    cookie: "",
  };
  win.atob = (s) => Buffer.from(s, "base64").toString("binary");
  win.btoa = (s) => Buffer.from(s, "binary").toString("base64");
  win.setTimeout = setTimeout;
  win.clearTimeout = clearTimeout;
  win.setInterval = setInterval;
  win.clearInterval = clearInterval;
  return win;
}

function encrypt(token, version, payload) {
  const win = buildWindow(token, version);
  const src = fs.readFileSync(path.join(__dirname, "chaos_vm.js"), "utf8");
  const ctx = vm.createContext(win);
  vm.runInContext(src, ctx, { timeout: 5000 });

  if (typeof win.xMidas !== "function") {
    throw new Error("window.xMidas did not initialise");
  }
  // Prime once (matches the page's window.xMidas() init call). Its internal
  // init may throw on a missing browser global; that is harmless for encrypt.
  try {
    win.xMidas();
  } catch (e) {}

  const json = JSON.stringify(payload);
  const hex = win.xMidas({ d: json });
  if (!hex || typeof hex !== "string" || hex.length === 0) {
    throw new Error("xMidas returned empty result");
  }
  const encrypt_msg = Buffer.from(hex, "hex").toString("base64");
  return {
    ok: true,
    encrypt_msg,
    ctoken: token,
    ctoken_ver: version || "1.0.1",
    hex_len: hex.length,
  };
}

function main() {
  let input = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (d) => (input += d));
  process.stdin.on("end", () => {
    let out;
    try {
      const req = JSON.parse(input);
      if (!req.token) throw new Error("missing token");
      out = encrypt(req.token, req.version, req.payload || {});
    } catch (e) {
      out = { ok: false, error: String((e && e.message) || e) };
    }
    process.stdout.write(JSON.stringify(out) + "\n");
  });
}

main();
