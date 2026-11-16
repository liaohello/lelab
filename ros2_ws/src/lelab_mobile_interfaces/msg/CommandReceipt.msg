# A receipt is not proof of motion. publisher_sent means the transport handed
# the frame to the car control process; emitted means its control tick wrote a
# target; controller_acknowledged and execution_observed stay unavailable until
# the STM32 protocol carries a real acknowledgement.
std_msgs/Header header

string data_identity
string transport               # frame_stream_v1 | dds_v1
string session_id
uint64 session_epoch
string stream_epoch
uint64 frame_seq
uint64 sample_seq
string state                   # admitted | refused | emitted | stopped
string reason
string detail

bool admitted
bool publisher_sent
bool emitted
string controller_acknowledged  # unavailable
string execution_observed       # unavailable

int64 received_mono_ns
int64 deadline_mono_ns
float64 window_remaining_ms
float64 window_age_ms
