// Loads the board in headless Chrome and reports page errors plus a few DOM facts, as text (no screenshots).
//   node tests/page_check.mjs <url> '<js expression returning JSON-able value>' ...
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [url, ...exprs] = process.argv.slice(2);
const chrome = process.env.CHROME || ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "google-chrome", "chromium", "chromium-browser"].find(Boolean);
const dir = mkdtempSync(join(tmpdir(), "sg-chrome-"));
const port = 9300 + Math.floor(Math.random() * 500);
const proc = spawn(chrome, ["--headless=new", "--disable-gpu", "--no-first-run", `--user-data-dir=${dir}`, `--remote-debugging-port=${port}`, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let targets;
for (let i = 0; i < 50 && !targets; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch { await sleep(200); } }
const ws = new WebSocket(targets.find((t) => t.type === "page").webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));
let id = 0; const pending = new Map(); const errors = [];
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
  if (d.method === "Runtime.exceptionThrown") errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
  if (d.method === "Runtime.consoleAPICalled" && d.params.type === "error") errors.push("console.error: " + d.params.args.map((a) => a.value ?? a.description).join(" "));
};
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
await send("Runtime.enable"); await send("Page.enable");
await send("Page.navigate", { url });
await sleep(Number(process.env.WAIT || 3500));
for (const e of exprs) {
  const r = await send("Runtime.evaluate", { expression: e, returnByValue: true, awaitPromise: true });
  console.log(e.slice(0, 70).padEnd(72), "=>", JSON.stringify(r.result?.result?.value ?? r.result?.exceptionDetails?.text));
}
console.log("page errors:", errors.length ? errors : "none");
ws.close(); proc.kill(); await sleep(800); try { rmSync(dir, { recursive: true, force: true }); } catch {}
process.exit(0);
