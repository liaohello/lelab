# The existing teleop_frame_v1 envelope carried on DDS.
# No raw control token is present: auth_ticket is an HMAC over these fields
# using a pre-shared key provisioned out of band. The arm and gripper commands
# are flattened but otherwise identical to command_v1.
uint8 frame_protocol_version
uint64 frame_seq
string stream_epoch
string session_id
uint64 session_epoch
string calibration_bundle_hash
uint64 sample_seq
string freshness_window_id

string sender_id
string auth_key_id
string auth_ticket
string transport_epoch

# arm command
string source
uint64 arm_seq
uint32 arm_ttl_ms
string[] joint_names
float64[] positions_rad
bool has_leader_rad
float64[] leader_rad
bool has_pre_clip_rad
float64[] pre_clip_rad
bool arm_clutch
string arm_clutch_label

# gripper command
uint64 gripper_seq
uint32 gripper_ttl_ms
string gripper_joint
float64 gripper_rad
bool gripper_clutch
string gripper_clutch_label
