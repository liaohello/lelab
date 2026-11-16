# One complete measured six-servo scan from the single board owner.
# Arrays are fixed to the arm+gripper order joint1..joint5, r_joint (ids 1..5,10).
# raw is the signed servo position in ticks; position_rad is the owner's
# calibration-backed conversion. A command cache is never published here.
std_msgs/Header header

string source                  # ros_robot_controller/servo_readback
string data_identity           # simulated_offline | hardware
uint8 frame_format             # 2 = measured readback envelope v2
uint32 owner_instance          # software owner instance, not an MCU boot id
uint64 scan_sequence
uint64 scan_span_us
int64 sample_start_mono_ns     # owner monotonic clock at scan start
int64 sample_end_mono_ns       # owner monotonic clock after the last read
int64 published_mono_ns        # owner monotonic clock before publish
int64 sample_age_ns            # published_mono_ns - sample_end_mono_ns
string clock_domain            # car_process_monotonic
string clock_epoch             # identifies this owner's monotonic epoch
string calibration_hash

string[6] joint_names
uint8[6] servo_ids
int32[6] raw
float64[6] position_rad
bool[6] valid
string[6] errors
