# Owner acquisition health and consumer receipt health are separate facts.
# consumer_failed_scans counts invalid received frames; scans_failed counts
# failed acquisition scans on the owner. consumer failure is not owner failure.
std_msgs/Header header

string source
string data_identity
string owner_identity_kind     # software_owner_instance
uint32 owner_instance
uint64 scan_sequence

float64 period_s
float64 quiet_s
float64 transaction_budget_s
uint64 scans_started
uint64 scans_ok
uint64 scans_failed
uint32 consecutive_failures
uint64 write_failures
int64 last_success_seq
int64 last_success_mono_ns
float64 last_success_age_s
float64 last_scan_interval_s
float64 max_scan_interval_s
bool stale

DriverFailure first_failure
DriverFailure last_failure

string last_recovery_phase
bool last_recovery_ok
float64 last_recovery_quiet_s
float64 last_recovery_duration_ms
int64 last_recovery_mono_ns

uint64 consumer_successful_scans
uint64 consumer_failed_scans
string consumer_last_error
bool consumer_first_failure_present
string consumer_first_failure_reason
int64 consumer_first_failure_mono_ns

string clock_domain
string clock_epoch
