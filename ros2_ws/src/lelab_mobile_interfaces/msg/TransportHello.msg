# Open one DDS control epoch without publishing the raw lease token.
# The ticket is HMAC-SHA256 over the fields below with a pre-shared key.
string transport               # dds_v1
string session_id
uint64 session_epoch
string calibration_bundle_hash
string sender_id
string auth_key_id
string auth_ticket
string transport_epoch
