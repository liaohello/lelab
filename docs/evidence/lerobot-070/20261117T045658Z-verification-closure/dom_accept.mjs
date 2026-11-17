/**
 * K5 / UI-DOM-01 — production-bundle DOM acceptance.
 *
 * Drives the built SPA (frontend/dist) through a real Chromium: landing →
 * training form → FastWAM dimensions → dataset pick → submit → job page →
 * inference dialog with a Chinese task and remote deployment identity.
 *
 * Every assertion is checked against both the DOM and the harness request log
 * (`/__ui_log`), so a green check means the browser really sent the contract —
 * not that a route returned 200. No hardware, no runner, no SSH: the stubs
 * validate with the real LeLab pydantic models and write nothing.
 *
 * Usage: node dom_accept.mjs   (exit 0 = all assertions passed)
 */
import { writeFileSync } from "node:fs";
import { chromium } from "playwright";

const BASE = process.env.DOM_BASE ?? "http://127.0.0.1:8138";
const OUT = process.env.DOM_OUT ?? "/home/lfy/lelab-rmr/closure";

const results = [];
const consoleErrors = [];
const pageErrors = [];

function check(name, ok, detail = "") {
  results.push({ name, ok: Boolean(ok), detail: String(detail).slice(0, 400) });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
}

async function uiLog(page) {
  const res = await page.request.get(`${BASE}/__ui_log?limit=500`);
  return (await res.json()).entries;
}

async function posts(page, route) {
  return (await uiLog(page)).filter((e) => e.route === route);
}

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1200 } });
// The job page reads the selected robot from localStorage; useRobots then maps
// it against GET /robots. Pre-seeding is what an operator does by picking a
// robot on the landing page, minus the click.
await context.addInitScript(() => {
  localStorage.setItem("lelab.selectedRobot", "so101-red");
});
const page = await context.newPage();
page.on("console", (m) => {
  if (m.type() === "error") consoleErrors.push(m.text().slice(0, 200));
});
page.on("pageerror", (e) => pageErrors.push(String(e).slice(0, 300)));

try {
  // ---- 1. Landing -------------------------------------------------------
  await page.goto(`${BASE}/`, { waitUntil: "load" });
  await page.waitForSelector("#root", { timeout: 20000 });
  check("landing renders", (await page.locator("#root").innerHTML()).length > 100);
  const trainEntry = page.getByRole("button", { name: "Training", exact: true });
  check("landing exposes a Training entry", (await trainEntry.count()) > 0);

  await trainEntry.first().click();
  await page.waitForURL("**/training", { timeout: 15000 });
  await page.waitForSelector("#policy_type", { timeout: 15000 });
  check("training form opens from the UI", page.url().endsWith("/training"));

  const defaultPolicy = (await page.locator("#policy_type").innerText()).trim();
  check("policy defaults to ACT", /ACT/i.test(defaultPolicy), defaultPolicy);

  // ---- 2. Model switch reveals the FastWAM dimension contract ----------
  check("dimension inputs hidden before switching", (await page.locator("#policy_action_dim").count()) === 0);
  await page.locator("#policy_type").click();
  await page.getByRole("option", { name: /FastWAM/ }).click();
  await page.waitForSelector("#policy_action_dim", { timeout: 8000 });
  const dimDefaults = {
    action: await page.locator("#policy_action_dim").inputValue(),
    proprio: await page.locator("#policy_proprio_dim").inputValue(),
  };
  check(
    "FastWAM dims start empty so 7/8 is never silently inherited",
    dimDefaults.action === "" && dimDefaults.proprio === "",
    JSON.stringify(dimDefaults),
  );

  // ---- 3. Missing dataset keeps Start disabled -------------------------
  const startBtn = page.getByRole("button", { name: /Start Training/ });
  check("Start is disabled with no dataset selected", await startBtn.isDisabled());

  // ---- 4. 6-dim SO-101 single arm -------------------------------------
  await page.locator("#policy_action_dim").fill("6");
  await page.locator("#policy_proprio_dim").fill("6");
  await page.locator('button[role="combobox"]', { hasText: "Select one or more datasets" }).click();
  await page.getByRole("option", { name: /so101\/pick_cube_local/ }).click();
  await page.keyboard.press("Escape");
  check("dataset selection unblocks Start", await startBtn.isEnabled());

  await page.screenshot({ path: `${OUT}/dom-01-training-filled.png`, fullPage: true });

  const trainBaseline = (await posts(page, "/jobs/training")).length;
  await startBtn.click();
  await page.waitForURL("**/training/fake-rmr-001", { timeout: 20000 });
  await page.waitForTimeout(1500);
  const trains = (await posts(page, "/jobs/training")).slice(trainBaseline);
  const accepted = trains.filter((e) => e.status === 201);
  const cfg = accepted.at(-1)?.body?.config ?? {};
  check("browser POSTed exactly one training job", trains.length === 1, `posts=${trains.length}`);
  check(
    "wire carries fastwam + 6/6 dims from the DOM",
    cfg.policy_type === "fastwam" && cfg.policy_action_dim === 6 && cfg.policy_proprio_dim === 6,
    JSON.stringify({ p: cfg.policy_type, a: cfg.policy_action_dim, s: cfg.policy_proprio_dim }),
  );
  check("wire carries the dataset chosen in the DOM", cfg.dataset_repo_id === "so101/pick_cube_local", cfg.dataset_repo_id);
  check("job page opens with the fake job", (await page.locator("body").innerText()).includes("fastwam-so101-6dim"));
  await page.screenshot({ path: `${OUT}/dom-02-job-monitor.png`, fullPage: true });

  // ---- 5. Inference dialog: remote identity / units / dims / timeout ---
  const runOnRobot = page.getByRole("button", { name: /Run on robot/ });
  await runOnRobot.waitFor({ timeout: 15000 });
  check("Run on robot enabled once a checkpoint exists", await runOnRobot.isEnabled());
  await runOnRobot.click();
  const dialog = page.locator('[role="dialog"]');
  await dialog.waitFor({ timeout: 10000 });
  check("inference dialog opens", await dialog.isVisible());

  const targetSelect = dialog.getByRole("combobox").filter({ hasText: /Jetson|Train server/ }).first();
  await targetSelect.click();
  await page.getByRole("option", { name: /Train server/ }).click();
  await dialog.locator("#remoteDeployment").waitFor({ timeout: 8000 });
  check("remote target reveals deployment identity fields", true);

  const dimValues = {
    state: await dialog.locator("#remoteStateDim").inputValue(),
    action: await dialog.locator("#remoteActionDim").inputValue(),
    timeout: await dialog.locator("#remoteActionTimeout").inputValue(),
  };
  check(
    "remote dims default to the SO-101 6/6 pair",
    dimValues.state === "6" && dimValues.action === "6",
    JSON.stringify(dimValues),
  );

  const startInference = dialog.getByRole("button", { name: /Start Inference/ });
  check("Start Inference blocked until deployment/semantics are named", await startInference.isDisabled());

  await dialog.locator("#remoteDeployment").fill("so101_red");
  await dialog.locator("#remoteSemantics").fill("so101-degrees-gripper-percent-v1");
  const TASK = "把红色方块放进盒子里";
  await dialog.locator("#task").fill(TASK);
  await dialog.locator("#remoteActionTimeout").fill("6.5");
  check("Start Inference enabled with a complete remote contract", await startInference.isEnabled());

  await page.screenshot({ path: `${OUT}/dom-03-inference-remote.png`, fullPage: true });
  const inferBaseline = (await posts(page, "/start-inference")).length;
  await startInference.click();
  await page.waitForURL("**/inference", { timeout: 20000 });
  await page.waitForTimeout(800);

  const inferPosts = (await posts(page, "/start-inference")).slice(inferBaseline);
  const infers = inferPosts.filter((e) => e.status === 200);
  const inf = infers.at(-1)?.body ?? {};
  check("browser POSTed exactly one inference request", inferPosts.length === 1, `posts=${inferPosts.length}`);
  check("Chinese task survives JS → JSON → schema", inf.task === TASK, JSON.stringify(inf.task));
  check(
    "remote identity/units travel verbatim",
    inf.remote_deployment === "so101_red" && inf.remote_semantics === "so101-degrees-gripper-percent-v1",
    JSON.stringify({ d: inf.remote_deployment, s: inf.remote_semantics }),
  );
  check(
    "remote dims + timeout come from the DOM",
    inf.remote_state_dim === 6 && inf.remote_action_dim === 6 && inf.remote_action_timeout_s === 6.5,
    JSON.stringify({ s: inf.remote_state_dim, a: inf.remote_action_dim, t: inf.remote_action_timeout_s }),
  );
  check("policy type is carried for the remote server", inf.policy_type === "fastwam", inf.policy_type);
  check(
    "follower port/config come from the selected robot",
    inf.follower_port === "/dev/lelab-fake-follower" && inf.follower_config === "so101_follower",
    JSON.stringify({ p: inf.follower_port, c: inf.follower_config }),
  );
  await page.screenshot({ path: `${OUT}/dom-04-inference-started.png`, fullPage: true });

  // ---- 6. API rejection surfaces in the UI -----------------------------
  await page.goto(`${BASE}/training`, { waitUntil: "load" });
  await page.waitForSelector("#policy_type", { timeout: 15000 });
  check("hard navigation to /training serves the SPA shell", (await page.locator("#policy_type").count()) === 1);
  await page.locator("#policy_type").click();
  await page.getByRole("option", { name: /FastWAM/ }).click();
  await page.waitForSelector("#policy_action_dim");
  await page.locator('button[role="combobox"]', { hasText: "Select one or more datasets" }).click();
  await page.getByRole("option", { name: /so101\/pick_cube_local/ }).click();
  await page.keyboard.press("Escape");
  await page.locator("#policy_action_dim").fill("");
  const postsBefore = (await posts(page, "/jobs/training")).length;
  await page.getByRole("button", { name: /Start Training/ }).click();
  await page.waitForTimeout(2500);
  const after = await posts(page, "/jobs/training");
  const rejected = after.slice(postsBefore).filter((e) => e.status === 400);
  check("cleared dimension reaches the backend and is refused", rejected.length === 1, `posts=${after.length - postsBefore}`);
  check(
    "backend refusal names the missing FastWAM dimension",
    /fastwam requires explicit dimensions/.test(rejected.at(-1)?.note ?? "") &&
      /policy_action_dim/.test(JSON.stringify(rejected.at(-1)?.note ?? "")),
    rejected.at(-1)?.note ?? "",
  );
  const bodyText = await page.locator("body").innerText();
  check("refusal is shown to the operator, not swallowed", /fastwam requires explicit dimensions/.test(bodyText));
  check("failed submit stays on the form", page.url().endsWith("/training"), page.url());
  await page.screenshot({ path: `${OUT}/dom-05-api-rejection.png`, fullPage: true });

  // Network stubs (the deliberate 400 above, and the harness's missing
  // WebSocket) show up as console messages; uncaught JS exceptions do not.
  const unexpected = consoleErrors.filter((t) => !/400 \(Bad Request\)|WebSocket/.test(t));
  check("no uncaught JS exception during the flow", pageErrors.length === 0, pageErrors.join(" | "));
  check("console errors are limited to the deliberate refusal", unexpected.length === 0, unexpected.join(" | "));
} catch (err) {
  check("flow completed without an exception", false, String(err?.stack ?? err));
} finally {
  const summary = {
    kind: "ui-dom-acceptance",
    base: BASE,
    ran_at: new Date().toISOString(),
    passed: results.filter((r) => r.ok).length,
    failed: results.filter((r) => !r.ok).length,
    console_errors: consoleErrors,
    page_errors: pageErrors,
    assertions: results,
  };
  writeFileSync(`${OUT}/dom-acceptance.json`, `${JSON.stringify(summary, null, 2)}\n`);
  await browser.close();
  console.log(`\n${summary.passed} passed, ${summary.failed} failed`);
  process.exit(summary.failed === 0 ? 0 : 1);
}
