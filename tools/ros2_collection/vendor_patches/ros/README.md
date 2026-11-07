# T1 ROS driver patch (offline candidate)

The target is the **existing** `ros_robot_controller` Board owner, not a second serial reader. Baseline sources are the two files in `/home/liaozhengyan/Desktop/resource/lander_pi/src/driver/ros_robot_controller/ros_robot_controller`; SHA-256 values are pinned in `patch_guard.py`. This is a source-derived candidate, not proof that the car runs these bytes. Verify deployment ownership and source identity before use. Do not apply it to the reference tree. Work with a stopped service and a backed-up, explicitly selected deployment copy; no deployment or device access was performed here.

```
python3 patch_guard.py /path/to/selected/ros_robot_controller check
python3 patch_guard.py /path/to/selected/ros_robot_controller apply
python3 patch_guard.py /path/to/selected/ros_robot_controller check-reverse
python3 patch_guard.py /path/to/selected/ros_robot_controller reverse
```

The guard refuses symlinked files and any SHA mismatch before invoking `patch`. It checks the entire baseline, checks patch applicability first, and checks the resulting SHA. Preserve backups: if patch itself fails partway, restore both files from the backup. No service restart is included.

Bus position request uses subcommand 0x05 and unicast ID; reply `<BBbh` contains ID, subcommand, status and signed raw position. Reads serialize, bound lock acquisition and queue wait, verify size/ID/subcommand/status, and latch faults or unsolicited/duplicate replies. Writes serialize and have a serial write timeout. Default read timeout is 0.1 s and may be configured when constructing `Board` (not exposed as ROS parameter yet). This can make six reads far slower than 50 Hz; measure actual latency and scan span before selecting a recording rate. Broadcast ID discovery is deliberately refused. The node reports failed reads rather than success. PWM read still has its original blocking behavior but no longer holds the bus read lock.

The protocol has no transaction ID. A late identical reply arriving during a later identical request after an apparently successful read is indistinguishable from the new reply. The patch cannot prove freshness or wire-level exactly-once behavior; a timeout or anomaly permanently latches reads for that instance. Restart alone is not proof the physical link has been drained; recovery requires an independently validated transport reset/quiescence or a protocol with transaction IDs. Position feedback, actual bounds, stop behavior, ROS integration and hardware throughput remain unverified. Writes and stop actions are not governed by this read patch; an independently hosted deadline-enforcing output owner is still required before real output.
