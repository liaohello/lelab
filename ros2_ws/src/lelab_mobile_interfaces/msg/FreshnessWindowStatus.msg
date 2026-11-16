# A freshness window issued on the car's own monotonic clock. The sender names
# this id in TeleopFrame; remaining_ms_at_publish is only a hint. The car
# resolves the id against its live window ring and its own deadline.
std_msgs/Header header

string session_id
uint64 session_epoch
string stream_epoch
string window_id
uint32 duration_ms
float64 remaining_ms_at_publish
int64 issued_mono_ns
int64 deadline_mono_ns
string clock_domain
string clock_epoch
bool command_transport_ready
string reason
