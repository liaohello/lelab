#include "t2_protocol.h"
#include <string.h>

/* Portable compiler barrier. The MCU integration additionally needs a DMB on
 * ARM (see t2_board.c); nothing here touches a UART, malloc or an RTOS queue. */
#if defined(__arm__) && defined(__GNUC__)
#define T2_BARRIER() __asm__ __volatile__("dmb" ::: "memory")
#elif defined(__GNUC__)
#define T2_BARRIER() __asm__ __volatile__("" ::: "memory")
#else
#define T2_BARRIER() ((void)0)
#endif

static uint32_t rd32(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1]<<8 | (uint32_t)p[2]<<16 | (uint32_t)p[3]<<24; }
static uint64_t rd64(const uint8_t *p) { return (uint64_t)rd32(p) | (uint64_t)rd32(p+4)<<32; }
static void wr32(uint8_t *p, uint32_t x) { unsigned i; for(i=0;i<4;i++) p[i]=(uint8_t)(x>>(8*i)); }
static void wr64(uint8_t *p, uint64_t x) { unsigned i; for(i=0;i<8;i++) p[i]=(uint8_t)(x>>(8*i)); }

uint8_t t2_crc8(const uint8_t *p, size_t n) {
    uint8_t c=0; size_t i; unsigned j;
    for(i=0;i<n;i++) { c^=p[i]; for(j=0;j<8;j++) c=(uint8_t)((c>>1)^((c&1u)?0x8cu:0)); }
    return c;
}
#define crc8 t2_crc8

static void zero_targets(t2_state *s) { unsigned i; for(i=0;i<4;i++) s->target[i]=0.0f; }

int t2_init(t2_state *s, const uint8_t boot[16]) { return t2_init_ex(s, boot, 0); }
int t2_init_ex(t2_state *s, const uint8_t boot[16], int allow_maintenance) {
    unsigned i; uint8_t any=0; if(!s || !boot) return 0;
    for(i=0;i<16;i++) any |= boot[i];
    memset(s,0,sizeof(*s)); if(!any) return 0;
    memcpy(s->boot,boot,16); s->boot_valid=1; s->faults=T2_FAULT_STOPPED;
    s->allow_maintenance=(uint8_t)(allow_maintenance?1:0);
    s->last_reject=T2_OK; return 1;
}

void t2_expire(t2_state *s, uint32_t now) {
    /* wrap-safe signed difference; must run every 10 ms tick even with no UART traffic */
    if(s && s->enabled && (int32_t)(now-s->deadline_ms)>=0) {
        zero_targets(s); s->enabled=0;
        s->faults |= T2_FAULT_TIMEOUT|T2_FAULT_STOPPED;
        s->latched_faults |= T2_FAULT_LATCH_TIMEOUT;
    }
}

void t2_force_stop(t2_state *s) {
    if(!s) return;
    zero_targets(s); s->enabled=0;
    s->faults |= T2_FAULT_STOPPED;
    s->latched_faults |= T2_FAULT_LATCH_ESTOP;
}
int t2_gate_legacy(const t2_state *s) { return (s && s->boot_valid && s->activated) ? 1 : 0; }
t2_result t2_last_reject(const t2_state *s) { return s ? s->last_reject : T2_REJ_NULL; }

const char *t2_result_name(t2_result r) {
    static const char *const names[] = {
        "OK","REJ_NULL","REJ_SHORT","REJ_HEADER","REJ_FUNCTION","REJ_LENGTH",
        "REJ_CRC","REJ_SUBCMD","REJ_BOOT","REJ_ACTION","REJ_EPOCH_ZERO",
        "REJ_SEQ_ZERO","REJ_TTL","REJ_COUNT","REJ_MOTOR_ID","REJ_DUP_ID",
        "REJ_NAN_INF","REJ_SPEED_RANGE","REJ_ENABLE_NONZERO","REJ_EPOCH_OLD",
        "REJ_EPOCH_MISMATCH","REJ_SEQ","REJ_NOT_ENABLED","REJ_ALREADY_ENABLED",
        "REJ_DISABLED_BOOT"
    };
    if((int)r < 0 || (size_t)r >= sizeof(names)/sizeof(names[0])) return "REJ_UNKNOWN";
    return names[r];
}

static t2_result reject(t2_state *s, t2_result r) {
    s->last_reject=r; s->reject_count++;
    s->faults |= T2_FAULT_REJECTED;
    s->latched_faults |= T2_FAULT_LATCH_REJECT;
    return r;
}

t2_result t2_command_ex(t2_state *s, const uint8_t *f, size_t len, uint32_t now) {
    const uint8_t *p; uint64_t epoch; uint32_t seq, bits; uint16_t ttl;
    uint8_t action, count, seen=0; float value[4]; unsigned i; uint8_t n;
    if(!s) return T2_REJ_NULL;
    if(!s->boot_valid) return T2_REJ_DISABLED_BOOT;
    t2_expire(s,now);  /* a stale session is stopped even if this frame is bad */
    if(!f || len<5) return reject(s,T2_REJ_SHORT);
    if(f[0]!=0xaa || f[1]!=0x55) return reject(s,T2_REJ_HEADER);
    if(f[2]!=T2_FUNCTION) return reject(s,T2_REJ_FUNCTION);
    n=f[3];
    if(len!=(size_t)n+5 || n<T2_CMD_HEADER_LEN) return reject(s,T2_REJ_LENGTH);
    if(crc8(f+2,len-3)!=f[len-1]) return reject(s,T2_REJ_CRC);
    p=f+4;
    if(p[0]!=T2_SUB_COMMAND) return reject(s,T2_REJ_SUBCMD);
    if(memcmp(p+2,s->boot,16)!=0) return reject(s,T2_REJ_BOOT);
    action=p[1]; epoch=rd64(p+18); seq=rd32(p+26);
    ttl=(uint16_t)(p[30]|(uint16_t)p[31]<<8); count=p[32];
    if(action<T2_ACTION_ENABLE || action>T2_ACTION_RELEASE) return reject(s,T2_REJ_ACTION);
    if(!epoch) return reject(s,T2_REJ_EPOCH_ZERO);
    if(!seq) return reject(s,T2_REJ_SEQ_ZERO);

    if(action==T2_ACTION_CANCEL || action==T2_ACTION_RELEASE) {
        if(n!=T2_CMD_STOP_DATA_LEN || count!=0) return reject(s,T2_REJ_COUNT);
        if(ttl!=0) return reject(s,T2_REJ_TTL);
        if(action==T2_ACTION_CANCEL) {
            if(!s->enabled) return reject(s,T2_REJ_NOT_ENABLED);
            if(epoch!=s->highest_epoch) return reject(s,T2_REJ_EPOCH_MISMATCH);
            if(seq<=s->seq) return reject(s,T2_REJ_SEQ);
        } else {
            if(s->enabled) return reject(s,T2_REJ_ALREADY_ENABLED);
            if(epoch<=s->highest_epoch) return reject(s,T2_REJ_EPOCH_OLD);
        }
    } else {
        if(n!=T2_CMD_DRIVE_DATA_LEN || count!=T2_WHEELS) return reject(s,T2_REJ_COUNT);
        if(ttl<T2_MIN_TTL_MS || ttl>T2_MAX_TTL_MS) return reject(s,T2_REJ_TTL);
        for(i=0;i<T2_WHEELS;i++) {
            uint8_t id=p[33+5*i];
            if(id>=T2_WHEELS) return reject(s,T2_REJ_MOTOR_ID);
            if(seen&(1u<<id)) return reject(s,T2_REJ_DUP_ID);
            seen|=(uint8_t)(1u<<id);
            bits=rd32(p+34+5*i);
            if((bits&0x7f800000u)==0x7f800000u) return reject(s,T2_REJ_NAN_INF);
            memcpy(&value[id],&bits,4);
            if(value[id]>T2_MAX_ABS_RPS || value[id]<-T2_MAX_ABS_RPS) return reject(s,T2_REJ_SPEED_RANGE);
            if(action==T2_ACTION_ENABLE && (bits&0x7fffffffu)!=0) return reject(s,T2_REJ_ENABLE_NONZERO);
        }
        if(action==T2_ACTION_ENABLE) {
            if(s->enabled) return reject(s,T2_REJ_ALREADY_ENABLED);
            if(epoch<=s->highest_epoch) return reject(s,T2_REJ_EPOCH_OLD);
        } else {
            if(!s->enabled) return reject(s,T2_REJ_NOT_ENABLED);
            if(epoch!=s->highest_epoch) return reject(s,T2_REJ_EPOCH_MISMATCH);
            if(seq<=s->seq) return reject(s,T2_REJ_SEQ);
        }
    }
    /* accepted: no partial updates, deadline and targets change together */
    s->seq=seq; s->last_accepted_ms=now; s->last_reject=T2_OK;
    s->faults=0;   /* current status only; latched history survives re-enable */
    if(action==T2_ACTION_CANCEL) {
        zero_targets(s); s->enabled=0; s->faults=T2_FAULT_STOPPED; return T2_OK;
    }
    if(action==T2_ACTION_RELEASE) {
        zero_targets(s); s->enabled=0; s->highest_epoch=epoch;
        s->latched_faults=0; s->activated=0; s->faults=T2_FAULT_STOPPED; return T2_OK;
    }
    if(action==T2_ACTION_ENABLE) { s->highest_epoch=epoch; s->activated=1; }
    s->deadline_ms=now+ttl; s->enabled=1;
    for(i=0;i<T2_WHEELS;i++) s->target[i]=value[i];
    return T2_OK;
}
int t2_command(t2_state *s, const uint8_t *frame, size_t len, uint32_t now_ms) {
    return t2_command_ex(s,frame,len,now_ms)==T2_OK ? 1 : 0;
}

static size_t frame(uint8_t *o, size_t cap, const uint8_t *p, uint8_t n) {
    size_t total=(size_t)n+5; if(!o || cap<total) return 0;
    o[0]=0xaa; o[1]=0x55; o[2]=T2_FUNCTION; o[3]=n; memcpy(o+4,p,n); o[total-1]=crc8(o+2,n+2); return total;
}
size_t t2_capabilities(const t2_state *s, const uint8_t build[16], uint8_t *out, size_t cap) {
    uint8_t p[38]; if(!s || !s->boot_valid || !build) return 0;
    p[0]=T2_SUB_CAP_REPORT; p[1]=T2_PROTOCOL_VERSION;
    memcpy(p+2,s->boot,16); memcpy(p+18,build,16);
    wr32(p+34,T2_CAP_WHEELS|T2_CAP_TIMEOUT|T2_CAP_SESSION);
    return frame(out,cap,p,sizeof(p));
}
size_t t2_capabilities_query(uint8_t *out, size_t cap) {
    uint8_t p[2]; p[0]=T2_SUB_CAP_QUERY; p[1]=T2_PROTOCOL_VERSION;
    return frame(out,cap,p,sizeof(p));
}
size_t t2_snapshot(const t2_state *s, const t2_sample *v, uint8_t *out, size_t cap) {
    uint8_t p[93]; unsigned i; uint32_t bits;
    if(!s || !s->boot_valid || !v) return 0;
    if(!t2_sample_is_finite(v)) return 0;
    p[0]=T2_SUB_SNAPSHOT; memcpy(p+1,s->boot,16);
    wr32(p+17,v->tick_ms); wr32(p+21,v->sample_seq);
    wr32(p+25,v->faults|s->faults|s->latched_faults);
    for(i=0;i<T2_WHEELS;i++) {
        unsigned off=29+16*i;
        wr64(p+off,(uint64_t)v->count[i]);
        memcpy(&bits,&v->measured_rps[i],4); wr32(p+off+8,bits);
        memcpy(&bits,&v->target_rps[i],4); wr32(p+off+12,bits);
    }
    return frame(out,cap,p,sizeof(p));
}

/* ---- snapshot coherence ---- */
void t2_sampler_init(t2_sampler *m) {
    if(!m) { return; }
    memset((void *)&m->sample,0,sizeof(m->sample)); m->seq=0;
}
void t2_sampler_begin(t2_sampler *m, uint32_t generation) {
    if(!m) { return; }
    T2_BARRIER(); m->seq=(generation<<1)|1u; T2_BARRIER();
}
void t2_sampler_commit(t2_sampler *m) {
    if(!m) { return; }
    T2_BARRIER(); m->seq=m->seq+1u; T2_BARRIER();
}
void t2_sampler_publish(t2_sampler *m, const t2_sample *v) {
    uint32_t g; if(!m || !v) return;
    g=(m->seq>>1)+1u; t2_sampler_begin(m,g); m->sample=*v; t2_sampler_commit(m);
}
int t2_sampler_read_begin(const t2_sampler *m, t2_sample *out, uint32_t *gen) {
    uint32_t a; if(!m || !out || !gen) return 0;
    a=m->seq; T2_BARRIER();
    if(a&1u) return 0;             /* writer active: never publish a half frame */
    *out=m->sample; *gen=a; T2_BARRIER(); return 1;
}
int t2_sampler_read_end(const t2_sampler *m, uint32_t gen) {
    uint32_t b; if(!m) return 0; b=m->seq;
    return (b==gen && !(b&1u)) ? 1 : 0;
}
int t2_sampler_read(const t2_sampler *m, t2_sample *out) {
    uint32_t g; if(!t2_sampler_read_begin(m,out,&g)) return 0;
    return t2_sampler_read_end(m,g);
}

int t2_compose_count_checked(int64_t before, uint32_t counter, int64_t after,
                             int32_t ticks_overflow, int64_t *out) {
    int64_t period;
    if(!out || ticks_overflow<=0) return 0;
    period=(int64_t)ticks_overflow;
    if(counter>=(uint32_t)ticks_overflow) return 0;  /* ARR=60000 -> counter 0..60000 */
    if(before!=after) return 0;                     /* rollover raced the read */
    if(before>INT64_MAX/period || before<INT64_MIN/period) return 0;
    *out=before*period+(int64_t)counter;
    return 1;
}
int t2_sample_is_finite(const t2_sample *v) {
    unsigned i; uint32_t bits; if(!v) return 0;
    for(i=0;i<T2_WHEELS;i++) {
        memcpy(&bits,&v->measured_rps[i],4);
        if((bits&0x7f800000u)==0x7f800000u) return 0;
        memcpy(&bits,&v->target_rps[i],4);
        if((bits&0x7f800000u)==0x7f800000u) return 0;
    }
    return 1;
}
