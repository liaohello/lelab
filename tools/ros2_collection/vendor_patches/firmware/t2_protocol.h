#ifndef LELAB_T2_PROTOCOL_H
#define LELAB_T2_PROTOCOL_H
#include <stddef.h>
#include <stdint.h>

/* T2 Wave A completion. Wire layout is byte-compatible with the earlier
 * standalone candidate: golden_vectors.json keys enable/update/stop,
 * capabilities, snapshot, count_wrap, new_boot, bad_crc, wrong_function and
 * legacy_no_response are unchanged. See PROTOCOL.md. NOT an MCU build. */
#define T2_FUNCTION 12u
#define T2_PROTOCOL_VERSION 1u
#define T2_SUB_CAP_QUERY 0x01u
#define T2_SUB_COMMAND 0x02u
#define T2_SUB_CAP_REPORT 0x81u
#define T2_SUB_SNAPSHOT 0x82u

#define T2_CAP_WHEELS 1u
#define T2_CAP_TIMEOUT 2u
#define T2_CAP_SESSION 4u
#define T2_CAP_MASK 7u

#define T2_ACTION_ENABLE 1u
#define T2_ACTION_DRIVE 2u
#define T2_ACTION_CANCEL 3u
#define T2_ACTION_RELEASE 4u

#define T2_WHEELS 4u
#define T2_TICKS_OVERFLOW 60001   /* TIM ARR=60000 -> 60001 counts per rollover */
#define T2_MIN_TTL_MS 1u
#define T2_MAX_TTL_MS 500u
#define T2_MAX_ABS_RPS 6.0f       /* MOTOR_DEFAULT_RPS_LIMIT; config, not measurement */
#define T2_FRAME_MAX 110u
#define T2_CAP_DATA_LEN 38u
#define T2_SNAPSHOT_DATA_LEN 93u
#define T2_CMD_HEADER_LEN 33u
#define T2_CMD_DRIVE_DATA_LEN (T2_CMD_HEADER_LEN + 20u)  /* 53 */
#define T2_CMD_STOP_DATA_LEN T2_CMD_HEADER_LEN           /* 33 */

/* current status (low 16) */
#define T2_FAULT_TIMEOUT 1u
#define T2_FAULT_STOPPED 2u
#define T2_FAULT_ENCODER_INVALID 4u
#define T2_FAULT_SNAPSHOT_STALE 8u
#define T2_FAULT_REJECTED 16u
/* latched history (high 16); cleared only by T2_ACTION_RELEASE */
#define T2_FAULT_LATCH_TIMEOUT 0x00010000u
#define T2_FAULT_LATCH_REJECT 0x00020000u
#define T2_FAULT_LATCH_ESTOP 0x00040000u
#define T2_FAULT_LATCH_MASK 0xFFFF0000u

typedef enum {
    T2_OK = 0,
    T2_REJ_NULL,
    T2_REJ_SHORT,
    T2_REJ_HEADER,
    T2_REJ_FUNCTION,
    T2_REJ_LENGTH,
    T2_REJ_CRC,
    T2_REJ_SUBCMD,
    T2_REJ_BOOT,
    T2_REJ_ACTION,
    T2_REJ_EPOCH_ZERO,
    T2_REJ_SEQ_ZERO,
    T2_REJ_TTL,
    T2_REJ_COUNT,
    T2_REJ_MOTOR_ID,
    T2_REJ_DUP_ID,
    T2_REJ_NAN_INF,
    T2_REJ_SPEED_RANGE,
    T2_REJ_ENABLE_NONZERO,
    T2_REJ_EPOCH_OLD,
    T2_REJ_EPOCH_MISMATCH,
    T2_REJ_SEQ,
    T2_REJ_NOT_ENABLED,
    T2_REJ_ALREADY_ENABLED,
    T2_REJ_DISABLED_BOOT
} t2_result;

/* First fields are kept byte-for-byte from the earlier candidate so existing
 * host fixtures keep their layout. New fields are appended. */
typedef struct {
    uint8_t boot[16];
    uint64_t highest_epoch;
    uint32_t seq, deadline_ms, faults;
    uint8_t enabled, boot_valid;
    float target[4];
    uint32_t latched_faults;
    uint32_t reject_count;
    uint32_t last_accepted_ms;
    uint8_t activated;
    uint8_t allow_maintenance;
    uint8_t reserved[2];
    t2_result last_reject;
} t2_state;

typedef struct {
    int64_t count[4];
    float measured_rps[4], target_rps[4];
    uint32_t tick_ms, sample_seq, faults;
} t2_sample;

/* seqlock: even = stable, odd = writer active. ISR publishes, task reads. */
typedef struct {
    volatile uint32_t seq;
    t2_sample sample;
} t2_sampler;

/* Returns 0 for a zero boot id. A real build must supply a fresh 128-bit id. */
int t2_init(t2_state *s, const uint8_t boot[16]);
int t2_init_ex(t2_state *s, const uint8_t boot[16], int allow_maintenance);
/* Call from the periodic monotonic timer even when serial traffic stops. */
void t2_expire(t2_state *s, uint32_t now_ms);
/* Full frame incl. CRC. 1 only on accepted command, 0 otherwise. */
int t2_command(t2_state *s, const uint8_t *frame, size_t len, uint32_t now_ms);
/* Same, with a precise rejection reason; T2_OK means accepted. */
t2_result t2_command_ex(t2_state *s, const uint8_t *frame, size_t len, uint32_t now_ms);
t2_result t2_last_reject(const t2_state *s);
const char *t2_result_name(t2_result r);
/* Block legacy PACKET_FUNC_MOTOR speed writes while a T2 session is active. */
int t2_gate_legacy(const t2_state *s);
/* Unconditional stop from the legacy stop command (estop path). */
void t2_force_stop(t2_state *s);
/* Emit frames from a task, never from the motor ISR. */
size_t t2_capabilities(const t2_state *s, const uint8_t build_id[16], uint8_t *out, size_t cap);
size_t t2_capabilities_query(uint8_t *out, size_t cap);
size_t t2_snapshot(const t2_state *s, const t2_sample *sample, uint8_t *out, size_t cap);

/* Snapshot consistency + encoder wrap handling (real C, host-testable). */
void t2_sampler_init(t2_sampler *m);
void t2_sampler_begin(t2_sampler *m, uint32_t generation);
void t2_sampler_commit(t2_sampler *m);
void t2_sampler_publish(t2_sampler *m, const t2_sample *v);
/* Two-phase read: begin copies the coherent sample, end re-checks the seqlock.
 * Production calls t2_sampler_read(); the split lets a host prove that a
 * publish between the two phases is rejected (torn snapshot). */
int t2_sampler_read_begin(const t2_sampler *m, t2_sample *out, uint32_t *gen);
int t2_sampler_read_end(const t2_sampler *m, uint32_t gen);
int t2_sampler_read(const t2_sampler *m, t2_sample *out);
/* Rejects a torn read when overflow_num changed between the two reads. */
int t2_compose_count_checked(int64_t overflow_before, uint32_t counter,
                             int64_t overflow_after, int32_t ticks_overflow,
                             int64_t *out);
int t2_sample_is_finite(const t2_sample *v);
/* bitwise CRC-8, verified equivalent to the board's table-driven checksum_crc8 */
uint8_t t2_crc8(const uint8_t *p, size_t n);
#endif
