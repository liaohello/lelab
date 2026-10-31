"""Check an OpenPI LIBERO client environment, including real offscreen rendering."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import sys


def configure_libero(output):
    """Create task-local configuration before LIBERO can ask an interactive question."""
    import importlib.util
    import yaml

    spec = importlib.util.find_spec("libero")
    if spec is None:
        raise RuntimeError("LIBERO is not installed in this interpreter")
    root = Path(next(iter(spec.submodule_search_locations))) / "libero"
    config_dir = output / "libero_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "benchmark_root": str(root),
        "bddl_files": str(root / "bddl_files"),
        "init_states": str(root / "init_files"),
        "assets": str(root / "assets"),
        "datasets": str(output / "datasets"),
    }
    Path(paths["datasets"]).mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(paths), encoding="utf-8")
    os.environ["LIBERO_CONFIG_PATH"] = str(config_dir)
    return paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suite", default="libero_spatial")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    paths = configure_libero(args.output)

    import imageio.v2 as imageio
    import numpy as np
    from libero.libero import benchmark
    from libero.libero.envs import OffScreenRenderEnv

    suites = benchmark.get_benchmark_dict()
    counts = {name: suites[name]().n_tasks for name in
              ["libero_spatial", "libero_object", "libero_goal", "libero_10"]}
    suite = suites[args.suite]()
    task = suite.get_task(0)
    env = OffScreenRenderEnv(
        bddl_file_name=str(Path(paths["bddl_files"]) / task.problem_folder / task.bddl_file),
        camera_heights=256, camera_widths=256,
    )
    try:
        env.seed(7)
        env.reset()
        states = suite.get_task_init_states(0)
        obs = env.set_init_state(states[0])
        for _ in range(10):
            obs, reward, done, info = env.step([0.0] * 6 + [-1.0])
        shapes = {key: list(np.asarray(value).shape) for key, value in obs.items()}
        for key in ["agentview_image", "robot0_eye_in_hand_image"]:
            frame = np.ascontiguousarray(obs[key][::-1, ::-1])
            if frame.shape != (256, 256, 3) or frame.std() < 1:
                raise RuntimeError("Invalid or blank rendered camera: %s" % key)
            imageio.imwrite(str(args.output / (key + ".png")), frame)
        state = np.concatenate([obs["robot0_eef_pos"], np.zeros(3), obs["robot0_gripper_qpos"]])
        if state.shape != (8,):
            raise RuntimeError("Unexpected state dimension")
        report = {
            "status": "passed", "python": sys.version, "renderer": os.environ.get("MUJOCO_GL"),
            "suite_counts": counts, "suite": args.suite, "task_id": 0,
            "task": task.language, "init_state_count": len(states), "observation_shapes": shapes,
            "state_dim": 8, "action_dim": 7,
            "versions": {name: importlib.metadata.version(name) for name in
                         ["numpy", "mujoco", "robosuite", "torch", "imageio"]},
        }
        (args.output / "environment.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    finally:
        env.close()


if __name__ == "__main__":
    main()
