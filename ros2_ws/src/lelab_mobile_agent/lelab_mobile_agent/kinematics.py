"""Forward/inverse kinematics for the five-axis arm using the URDF chain.

The model is fixed for this robot revision.  Keeping the chain explicit makes
the scripted expert independent of MoveIt while still using the same joint
names and limits as the Gazebo controller.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"]
LIMITS = [(-2.09, 2.09)] * 5

# (translation, rpy) for fixed/joint origins, following the URDF.
CHAIN = [
    ("fixed", (0.0, 0.0, 0.054), (0.0, 0.0, 0.0), None),
    ("fixed", (-0.00442651562530816, 0.0, 0.018000006985512), (0.0, 0.0, 0.0), None),
    ("fixed", (-0.0308199337666247, -0.00800013814077391, 0.0599417668336678), (0.0, 0.0, 0.0), None),
    (
        "joint",
        (0.0798305157765817, 0.00796999989406598, -0.00881105926199899),
        (0.0, 0.0, 0.0),
        (0.0, -0.00872653549837298, 0.999961923064171),
    ),
    (
        "joint",
        (0.0, -0.0182247260841935, 0.0314158515121316),
        (0.0, -7.61524218770913e-05 + 0.026, -0.0087263139928871),
        (0.00859171009912819, -0.999923369532431, 0.00891277624995309),
    ),
    (
        "joint",
        (-0.000861228256676561, 0.00063586369729467, 0.0741172981619549),
        (0.000101751721200393, -0.0117273803809202 + 0.012, 0.000101743881436431),
        (0.00859391749791871, -0.999923336386447, 0.00891436659750842),
    ),
    (
        "joint",
        (0.0, 0.004643642205860946, 0.0741383282673596),
        (0.0, 8.12032780429439e-05 - 0.019, 0.0),
        (0.00859391749791871, -0.999923336386447, 0.00891436659750842),
    ),
    (
        "joint",
        (-0.000167622119569388, 0.0159322086601561, 0.0497506513723871),
        (-0.00872598172862301, -5.95338970154445e-05, 0.00872749801929325),
        (-0.000182847987243455, 0.0, -0.999999983283301),
    ),
    ("fixed", (0.0, 0.0, 0.076), (0.0, 0.0, 0.0), None),
]


def rpy(r: float, p: float, y: float) -> np.ndarray:
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]])
    ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
    rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
    return rz @ ry @ rx


def axis_angle(axis: tuple[float, float, float], angle: float) -> np.ndarray:
    a = np.asarray(axis, dtype=float)
    a = a / np.linalg.norm(a)
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(angle) * k + (1 - math.cos(angle)) * (k @ k)


def transform(translation, rotation) -> np.ndarray:
    out = np.eye(4)
    out[:3, :3] = rotation
    out[:3, 3] = translation
    return out


def fk(q: list[float] | np.ndarray, tool_offset: float = 0.0) -> np.ndarray:
    q = np.asarray(q, dtype=float)
    t = np.eye(4)
    joint_index = 0
    for kind, xyz, r, axis in CHAIN:
        t = t @ transform(xyz, rpy(*r))
        if kind == "joint":
            t = t @ transform((0, 0, 0), axis_angle(axis, q[joint_index]))
            joint_index += 1
    if tool_offset:
        t = t @ transform((0, 0, tool_offset), np.eye(3))
    return t


def fk_jacobian(q: list[float] | np.ndarray):
    """Return TCP transform and the 6x5 geometric Jacobian in world frame."""
    q = np.asarray(q, dtype=float)
    t = np.eye(4)
    joint_index = 0
    origins: list[np.ndarray] = []
    axes: list[np.ndarray] = []
    for kind, xyz, r, axis in CHAIN:
        t = t @ transform(xyz, rpy(*r))
        if kind == "joint":
            axis_world = t[:3, :3] @ (np.asarray(axis, dtype=float) / np.linalg.norm(axis))
            origins.append(t[:3, 3].copy())
            axes.append(axis_world)
            t = t @ transform((0, 0, 0), axis_angle(axis, q[joint_index]))
            joint_index += 1
    p = t[:3, 3]
    jac = np.zeros((6, 5))
    for i, (origin, axis_world) in enumerate(zip(origins, axes, strict=True)):
        jac[:3, i] = np.cross(axis_world, p - origin)
        jac[3:, i] = axis_world
    return t, jac


def rotation_vector(r: np.ndarray) -> np.ndarray:
    cos_angle = max(-1.0, min(1.0, (np.trace(r) - 1.0) / 2.0))
    angle = math.acos(cos_angle)
    if angle < 1e-7:
        return 0.5 * np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]])
    scale = angle / (2.0 * math.sin(angle))
    return scale * np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]])


def pose_error(t: np.ndarray, p_des: np.ndarray, r_des: np.ndarray) -> np.ndarray:
    pos = p_des - t[:3, 3]
    rot = rotation_vector(r_des @ t[:3, :3].T)
    return np.concatenate([pos, ORIENTATION_WEIGHT * rot])


def desired_orientation(yaw: float) -> np.ndarray:
    """Tool x points radially, y is tangential and z points down."""
    x = np.array([math.cos(yaw), math.sin(yaw), 0.0])
    z = np.array([0.0, 0.0, -1.0])
    y = np.cross(z, x)
    return np.column_stack([x, y, z])


ORIENTATION_WEIGHT = 0.12


def pose_error(t: np.ndarray, p_des: np.ndarray, r_des: np.ndarray) -> np.ndarray:
    pos = p_des - t[:3, 3]
    rot = ORIENTATION_WEIGHT * np.cross(t[:3, 2], r_des[:, 2])
    return np.concatenate([pos, rot])


def align_tool_yaw(q, r_des):
    """Use joint5 only (TCP lies on its axis) to align the finger opening."""
    q = np.asarray(q, dtype=float).copy()
    t = fk(q)
    cur = t[:3, 1]
    des = r_des[:3, 1]
    cur_yaw = math.atan2(cur[1], cur[0])
    des_yaw = math.atan2(des[1], des[0])
    delta = (des_yaw - cur_yaw + math.pi) % (2 * math.pi) - math.pi
    q[4] = (q[4] + delta + math.pi) % (2 * math.pi) - math.pi
    q[4] = max(LIMITS[4][0], min(LIMITS[4][1], q[4]))
    return q


def ik(
    p_des,
    r_des,
    seed=None,
    restarts: int = 24,
    iterations: int = 500,
    tol: float = 2e-3,
    rng: np.random.Generator | None = None,
) -> tuple[np.ndarray, float]:
    rng = rng or np.random.default_rng(7)
    p_des = np.asarray(p_des, dtype=float)
    seeds = [seed] if seed is not None else []
    seeds += [
        np.array([0.0, 0.84, -2.03, -1.19, 0.0]),
        np.array([0.0, 0.5, -1.4, -1.0, 0.0]),
        np.array([0.0, 1.1, -1.8, -1.0, 0.0]),
        np.array([-0.3, -1.3, -0.65, -1.0, -1.5]),
        np.array([-0.3, -1.47, 0.03, -1.6, 1.5]),
        np.array([0.5, -1.3, -0.65, -1.0, 0.0]),
        np.array([-0.4, -0.9, -1.4, -0.4, 0.0]),
    ]
    while len(seeds) < restarts:
        seeds.append(rng.uniform([-2.0, -1.8, -2.05, -2.05, -2.0], [2.0, 2.0, 2.0, 2.0, 2.0]))
    lam = 2e-2
    best_q, best_err = None, float("inf")
    for q0 in seeds[:restarts]:
        q = np.clip(np.asarray(q0, dtype=float), [a for a, _ in LIMITS], [b for _, b in LIMITS])
        for _ in range(iterations):
            t, jac = fk_jacobian(q)
            err = pose_error(t, p_des, r_des)
            err_norm = float(np.linalg.norm(err))
            if err_norm < tol:
                break
            jac[3:, :] *= ORIENTATION_WEIGHT
            jjt = jac @ jac.T + lam * np.eye(6)
            try:
                dq = jac.T @ np.linalg.solve(jjt, err)
            except np.linalg.LinAlgError:
                break
            norm = np.linalg.norm(dq)
            if norm > 0.2:
                dq *= 0.2 / norm
            q = np.clip(q + dq, [a for a, _ in LIMITS], [b for _, b in LIMITS])
        t_best = fk(q)
        pos_err = float(np.linalg.norm(t_best[:3, 3] - p_des))
        down = float(t_best[2, 2])
        score = pos_err + 0.02 * max(0.0, down + 0.2)
        if score < best_err:
            best_q, best_err = q.copy(), score
    if best_q is None:
        raise RuntimeError("IK failed")
    best_q = align_tool_yaw(best_q, r_des)
    t_best = fk(best_q)
    pos_err = float(np.linalg.norm(t_best[:3, 3] - p_des))
    return best_q, pos_err


def ik_position(p_des, r_des, seed=None, restarts: int = 24) -> tuple[np.ndarray, float]:
    """Compensate the 5-DOF position bias by re-aiming the target."""
    p_des = np.asarray(p_des, dtype=float)
    p_cmd = p_des.copy()
    best_q, best_pos = None, float("inf")
    for _ in range(4):
        q, _ = ik(p_cmd, r_des, seed=seed, restarts=restarts)
        actual = fk(q)[:3, 3]
        pos_err = float(np.linalg.norm(actual - p_des))
        if pos_err < best_pos:
            best_q, best_pos = q, pos_err
        if pos_err < 1.5e-3:
            break
        p_cmd += p_des - actual
        seed = q
    if best_q is None:
        raise RuntimeError("IK failed")
    return best_q, best_pos


@dataclass
class GraspPlan:
    pre_grasp: list[float]
    grasp: list[float]
    lifted: list[float]
    above_container: list[float]
    released: list[float]
    retreat: list[float]


def plan_pick_place(cube_xy, container_xy, approach_offset=0.06):
    """Return joint waypoints for a top grasp in the robot base frame."""
    cube_x = cube_xy[0] - approach_offset
    cube_y = cube_xy[1]
    cont_x = container_xy[0] - approach_offset
    cont_y = container_xy[1]
    yaw_cube = math.atan2(cube_y - 0.0, cube_x - 0.045)
    yaw_cont = math.atan2(cont_y - 0.0, cont_x - 0.045)
    r_cube = desired_orientation(yaw_cube)
    r_cont = desired_orientation(yaw_cont)
    pre, e1 = ik_position([cube_x, cube_y, 0.085], r_cube)
    grasp, e2 = ik_position([cube_x, cube_y, 0.030], r_cube, seed=pre)
    lifted, e3 = ik_position([cube_x, cube_y, 0.100], r_cube, seed=grasp)
    above, e4 = ik_position([cont_x, cont_y, 0.100], r_cont, seed=lifted)
    release, e5 = ik_position([cont_x, cont_y, 0.090], r_cont, seed=above)
    retreat, e6 = ik_position([cont_x, cont_y, 0.120], r_cont, seed=release)
    errors = [e1, e2, e3, e4, e5, e6]
    if max(errors) > 0.02:
        raise RuntimeError(f"IK position residuals too high: {errors}")
    return GraspPlan(pre.tolist(), grasp.tolist(), lifted.tolist(), above.tolist(), release.tolist(), retreat.tolist()), errors
