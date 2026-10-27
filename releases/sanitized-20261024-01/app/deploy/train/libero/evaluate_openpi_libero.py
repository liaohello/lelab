"""Run reproducible LIBERO episodes against the existing OpenPI websocket policy."""
import argparse
import collections
import importlib.util
import json
from pathlib import Path
import time

from probe_libero import configure_libero


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="lelab-libero-policy")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--suite", choices=["libero_spatial", "libero_object", "libero_goal", "libero_10"],
                        default="libero_spatial")
    parser.add_argument("--task-ids", default="0", help="Comma-separated IDs, or all")
    parser.add_argument("--episodes", type=int, default=1)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--replan-steps", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.episodes < 1 or args.replan_steps < 1:
        parser.error("episodes and replan-steps must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    configure_libero(args.output)

    import imageio.v2 as imageio
    import numpy as np
    from libero.libero import benchmark
    from openpi_client import image_tools
    from openpi_client.websocket_client_policy import WebsocketClientPolicy

    # Reuse the image's exact task construction and quaternion convention.
    spec = importlib.util.spec_from_file_location("openpi_libero_example", "/opt/openpi/examples/libero/main.py")
    example = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = example
    spec.loader.exec_module(example)
    np.random.seed(args.seed)
    suite = benchmark.get_benchmark_dict()[args.suite]()
    task_ids = list(range(suite.n_tasks)) if args.task_ids == "all" else [int(x) for x in args.task_ids.split(",")]
    if not task_ids or len(set(task_ids)) != len(task_ids) or any(x < 0 or x >= suite.n_tasks for x in task_ids):
        parser.error("task IDs must be unique and within the suite")
    client = WebsocketClientPolicy(args.host, args.port)
    max_steps = {"libero_spatial": 220, "libero_object": 280, "libero_goal": 300, "libero_10": 520}[args.suite]
    report = {
        "status": "running", "backend": "OpenPI", "suite": args.suite,
        "task_ids": task_ids, "episodes_per_task": args.episodes, "seed": args.seed,
        "replan_steps": args.replan_steps, "settle_steps": 10, "max_steps": max_steps,
        "action_mode": "relative_end_effector", "camera_rotation_degrees": 180,
        "policy_image_size": 224, "render_image_size": 256,
        "episodes": [], "policy_metadata": client.get_server_metadata(),
    }
    report_path = args.output / "evaluation.json"

    def save():
        completed = report["episodes"]
        report["completed_episodes"] = len(completed)
        report["successes"] = sum(ep["success"] for ep in completed)
        report["success_rate"] = report["successes"] / len(completed) if completed else None
        temp = report_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temp.replace(report_path)

    save()
    try:
        for task_id in task_ids:
            task = suite.get_task(task_id)
            states = suite.get_task_init_states(task_id)
            if args.episodes > len(states):
                raise ValueError("Requested more episodes than fixed initial states")
            env, language = example._get_libero_env(task, 256, args.seed)
            try:
                for episode in range(args.episodes):
                    env.reset()
                    obs = env.set_init_state(states[episode])
                    client.reset()
                    for _ in range(10):
                        obs, _, _, _ = env.step(example.LIBERO_DUMMY_ACTION)
                    plan = collections.deque()
                    frames = []
                    timings = []
                    success = False
                    started = time.monotonic()
                    for step in range(max_steps):
                        image = np.ascontiguousarray(obs["agentview_image"][::-1, ::-1])
                        wrist = np.ascontiguousarray(obs["robot0_eye_in_hand_image"][::-1, ::-1])
                        frames.append(image)
                        if not plan:
                            observation = {
                                "observation/image": image_tools.convert_to_uint8(image_tools.resize_with_pad(image, 224, 224)),
                                "observation/wrist_image": image_tools.convert_to_uint8(image_tools.resize_with_pad(wrist, 224, 224)),
                                "observation/state": np.concatenate([
                                    obs["robot0_eef_pos"], example._quat2axisangle(obs["robot0_eef_quat"]),
                                    obs["robot0_gripper_qpos"],
                                ]),
                                "prompt": str(language),
                            }
                            inference_started = time.monotonic()
                            actions = np.asarray(client.infer(observation)["actions"])
                            timings.append(time.monotonic() - inference_started)
                            if actions.ndim != 2 or actions.shape[1] != 7 or not np.isfinite(actions).all():
                                raise RuntimeError("Invalid policy actions: %r" % (actions.shape,))
                            if len(actions) < args.replan_steps:
                                raise RuntimeError("Policy action horizon is shorter than replan_steps")
                            plan.extend(actions[:args.replan_steps])
                        obs, _, done, _ = env.step(plan.popleft().tolist())
                        if done:
                            success = True
                            break
                    frames.append(np.ascontiguousarray(obs["agentview_image"][::-1, ::-1]))
                    video = "task_%02d_episode_%03d_%s.mp4" % (task_id, episode, "success" if success else "failure")
                    imageio.mimwrite(str(args.output / video), frames, fps=20, codec="libx264")
                    record = {
                        "task_id": task_id, "episode": episode, "initial_state_index": episode,
                        "language": str(language), "success": success, "steps": step + 1,
                        "duration_s": time.monotonic() - started, "policy_calls": len(timings),
                        "first_inference_s": timings[0], "mean_inference_s": float(np.mean(timings)),
                        "video": video,
                    }
                    report["episodes"].append(record)
                    save()
                    print(json.dumps(record), flush=True)
            finally:
                env.close()
        report["status"] = "complete"
        save()
        print(json.dumps({key: report[key] for key in
                          ["status", "completed_episodes", "successes", "success_rate"]}), flush=True)
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = "%s: %s" % (type(exc).__name__, exc)
        save()
        raise


if __name__ == "__main__":
    main()
