# One failed servo transaction, as recorded by the owner or its consumer.
bool present
uint8 servo_id
string command                 # bus_servo_read_position
string reason                  # read_timeout | reply_latch_busy | ...
float64 lock_wait_ms
float64 duration_ms
string recovery_phase          # pending_quiet_wait | quiet_wait | ...
int64 last_success_seq         # -1 when no scan has succeeded
float64 scan_interval_s
int64 at_mono_ns
string data_identity
