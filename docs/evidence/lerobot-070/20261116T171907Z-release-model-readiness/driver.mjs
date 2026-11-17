// R2 production-form acceptance driver.
// Exercises the verbatim-extracted `configToRequest` plus the frontend's own
// `startTrainingJob` against the harness backend (real StartTrainingBody schema
// + real FastWAM start gate; fake runner, nothing spawned).
import { startTrainingJob } from "./out/jobsApi.js";
import { configToRequest } from "./out/config_extract.js";
import http from "node:http";

const BASE = "http://127.0.0.1:8137";
// Node's undici fetch needs Wasm memory, which this host's 4 GiB RLIMIT_AS
// denies. The frontend's Fetcher contract only needs (url, init) -> {ok, status,
// json()}, so a stdlib-http fetcher keeps the client code identical while
// avoiding undici.
const fetcher = (url, init = {}) => new Promise((resolve, reject) => {
  const u = new URL(url);
  // apiRequest already JSON-stringifies `body`; send it verbatim.
  const payload = init.body != null ? String(init.body) : null;
  const req = http.request({
    hostname: u.hostname, port: u.port, path: u.pathname + u.search,
    method: init.method ?? (payload ? "POST" : "GET"),
    headers: {
      ...(payload ? { "content-type": "application/json", "content-length": Buffer.byteLength(payload) } : {}),
    },
  }, (res) => {
    let data = "";
    res.on("data", (c) => { data += c; });
    res.on("end", () => {
      let body = null;
      try { body = data ? JSON.parse(data) : null; } catch { body = data; }
      resolve({ ok: res.statusCode < 400, status: res.statusCode, json: async () => body, text: async () => data });
    });
  });
  req.on("error", reject);
  if (payload) req.write(payload);
  req.end();
});

function baseConfig(overrides) {
  return {
    dataset_repo_id: "local/demo6d",
    policy_type: "act",
    steps: 10,
    batch_size: 2,
    seed: 0,
    num_workers: 0,
    wandb_enable: false,
    policy_device: "cpu",
    ...overrides,
  };
}

async function submit(label, config) {
  const request = configToRequest(config);
  try {
    const record = await startTrainingJob(BASE, fetcher, request);
    console.log(`${label}: OK job=${record.job_id ?? record.id} policy=${request.policy_type}`);
    return { label, status: "accepted", request, record };
  } catch (e) {
    console.log(`${label}: REFUSED ${e.message.slice(0, 160)}`);
    return { label, status: "refused", error: e.message, request };
  }
}

// Inference-path scenarios: the same object shape InferenceModal.tsx sends
// (lines 268-285 of the built-from source), validated by the REAL
// InferenceRequest schema in the harness.
async function submitInference(label, payload) {
  try {
    const res = await fetcher(`${BASE}/start-inference`, {
      method: "POST", body: JSON.stringify(payload),
      headers: { "content-type": "application/json" },
    });
    const body = await res.json();
    if (!res.ok) {
      console.log(`${label}: REFUSED ${JSON.stringify(body.detail).slice(0, 140)}`);
      return { label, status: "refused" };
    }
    const req = body.request ?? {};
    console.log(`${label}: OK deployment=${req.remote_deployment} action_dim=${req.remote_action_dim} task=${req.task}`);
    return { label, status: "accepted", request: body.request };
  } catch (e) {
    console.log(`${label}: ERROR ${e.message}`);
    return { label, status: "error", error: String(e.message) };
  }
}

const remoteBase = {
  follower_port: "/dev/serial/by-id/usb-follower",
  follower_config: "follower_a",
  policy_ref: "/ckpt/fastwam/last",
  task: "pick the red block",
  cameras: { wrist: { type: "opencv", camera_index: 0, width: 640, height: 480, fps: 30 } },
  duration_s: 30,
  target: "remote",
  policy_type: "fastwam",
};

const results = [];
// 1. FastWAM with explicit 6/6 dims.
results.push(await submit("fastwam-6d", baseConfig({
  policy_type: "fastwam", policy_action_dim: 6, policy_proprio_dim: 6,
  policy_pretrained_path: "/ckpt/fastwam/last",
})));
// 2. FastWAM without dims -> real start gate must refuse.
results.push(await submit("fastwam-missing-dims", baseConfig({
  policy_type: "fastwam", policy_pretrained_path: "/ckpt/fastwam/last",
})));
// 3. Dimensions leaking to another policy -> refused.
results.push(await submit("dims-on-act", baseConfig({
  policy_type: "act", policy_action_dim: 6, policy_proprio_dim: 8,
})));
// 4. FineART with a Chinese task, local runner.
results.push(await submit("fineart-chinese-task", baseConfig({
  policy_type: "fineart_vla",
  policy_pretrained_path: "/ckpt/fineart/last",
  task: "捡起红色方块并放入盒子",
})));
// 5. Remote (ssh) target toggle.
results.push(await submit("fastwam-remote", baseConfig({
  policy_type: "fastwam", policy_action_dim: 12, policy_proprio_dim: 12,
  policy_pretrained_path: "/ckpt/fastwam/last",
  target: { runner: "ssh_remote" },
})));

// 6. Remote inference with the full modal field set.
results.push(await submitInference("remote-inference-full", {
  ...remoteBase,
  remote_deployment: "armA",
  remote_semantics: "so101-degrees-gripper-percent-v1",
  remote_state_dim: 6,
  remote_action_dim: 6,
  remote_refill_seconds: 0.5,
  remote_action_timeout_s: 5,
}));
// 7. Remote inference missing deployment/semantics -> schema must refuse.
results.push(await submitInference("remote-inference-missing-identity", {
  ...remoteBase,
  remote_state_dim: 6,
  remote_action_dim: 6,
}));

const report = {
  generated: new Date().toISOString(),
  results: results.map(({ label, status, request, error }) => ({
    label, status,
    error: error ?? undefined,
    sent_policy_type: request.policy_type,
    sent_action_dim: request.policy_action_dim,
    sent_proprio_dim: request.policy_proprio_dim,
    sent_task: request.task,
    sent_target: request.target,
  })),
};
console.log("REPORT " + JSON.stringify(report, null, 2));
