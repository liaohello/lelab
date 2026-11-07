/* T2 native host harness: compiles and EXECUTES the real t2_protocol.c logic.
 * Linux/x86-64 host check only. NOT an MCU build, NOT a flash, NOT hardware.
 * Build: t2_host_build.sh / t2_verify.py. Prints a single JSON report. */
#include "t2_protocol.h"
#include "t2_crc_reference.h"
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static char checks_buf[16384]; static size_t checks_len=0; static int first_check=1;
static char vectors_buf[8192]; static size_t vectors_len=0; static int first_vec=1;
static int g_total=0, g_failed=0;

static void append(char *buf, size_t *len, size_t cap, const char *s) {
    size_t n=strlen(s);
    if (*len+n+1>=cap) return;
    memcpy(buf+*len,s,n); *len+=n; buf[*len]='\0';
}
static void check(const char *name, int ok) {
    char line[512];
    g_total++; if(!ok) g_failed++;
    snprintf(line,sizeof(line),"%s  {\"name\":\"%s\",\"ok\":%s}",
             first_check?"":",\n",name,ok?"true":"false");
    append(checks_buf,&checks_len,sizeof(checks_buf),line); first_check=0;
}
static void hex(char *dst, const uint8_t *p, size_t n) {
    static const char d[]="0123456789abcdef"; size_t i;
    for(i=0;i<n;i++){ dst[2*i]=d[p[i]>>4]; dst[2*i+1]=d[p[i]&15]; } dst[2*n]='\0';
}
static void vector(const char *name, const uint8_t *p, size_t n) {
    char line[512], h[256];
    hex(h,p,n);
    snprintf(line,sizeof(line),"%s    \"%s\":\"%s\"",first_vec?"":",\n",name,h);
    append(vectors_buf,&vectors_len,sizeof(vectors_buf),line); first_vec=0;
}
static void vector_empty(const char *name) {
    char line[128];
    snprintf(line,sizeof(line),"%s    \"%s\":\"\"",first_vec?"":",\n",name);
    append(vectors_buf,&vectors_len,sizeof(vectors_buf),line); first_vec=0;
}
static void wr32(uint8_t *p, uint32_t x) { unsigned i; for(i=0;i<4;i++) p[i]=(uint8_t)(x>>(8*i)); }
static void wr64(uint8_t *p, uint64_t x) { unsigned i; for(i=0;i<8;i++) p[i]=(uint8_t)(x>>(8*i)); }
static size_t make_frame(uint8_t *f, uint8_t func, const uint8_t *payload, uint8_t n) {
    f[0]=0xaa; f[1]=0x55; f[2]=func; f[3]=n;
    memcpy(f+4,payload,n); f[n+4]=t2_crc8(f+2,(size_t)n+2);
    return (size_t)n+5;
}
static size_t make_command(uint8_t *f, const uint8_t *boot, uint8_t action,
                           uint64_t epoch, uint32_t seq, uint16_t ttl, const float *v) {
    uint8_t p[53]; uint8_t n=(action==1||action==2)?53:33; unsigned i;
    memset(p,0,sizeof(p));
    p[0]=2; p[1]=action; memcpy(p+2,boot,16);
    wr64(p+18,epoch); wr32(p+26,seq);
    p[30]=(uint8_t)(ttl&0xff); p[31]=(uint8_t)(ttl>>8); p[32]=(n==53)?4:0;
    if(n==53) for(i=0;i<4;i++){ p[33+5*i]=(uint8_t)i; memcpy(p+34+5*i,&v[i],4); }
    return make_frame(f,T2_FUNCTION,p,n);
}
static size_t make_snapshot(uint8_t *f, const uint8_t *boot, uint32_t tick, uint32_t seq,
                            uint32_t faults, const int64_t *counts) {
    static const float meas[4]={-0.5f,0.0f,0.5f,1.0f};
    static const float tgt[4]={0.0f,0.25f,0.5f,0.75f};
    uint8_t p[93]; unsigned i; uint32_t bits;
    memset(p,0,sizeof(p)); p[0]=0x82; memcpy(p+1,boot,16);
    wr32(p+17,tick); wr32(p+21,seq); wr32(p+25,faults);
    for(i=0;i<4;i++){
        unsigned off=29+16*i;
        wr64(p+off,(uint64_t)counts[i]);
        memcpy(&bits,&meas[i],4); wr32(p+off+8,bits);
        memcpy(&bits,&tgt[i],4); wr32(p+off+12,bits);
    }
    return make_frame(f,T2_FUNCTION,p,93);
}
static void reseal(uint8_t *f) { f[f[3]+4]=t2_crc8(f+2,(size_t)f[3]+2); }

int main(void) {
    uint8_t boot[16], boot2[16], zero[16]={0}, build[16];
    uint8_t f[128], out[128], q[16];
    float zeros[4]={0,0,0,0}, speeds[4]={0.0f,0.25f,0.5f,0.75f};
    float large[4]={0,0,0,0};
    int64_t c1[4]={59999,-1,0,123}, c2[4]={60001,-2,0,124}, c3[4]={0,0,0,0};
    size_t n, qlen; unsigned i;
    t2_state s, sb; t2_sample sample, rd, bad; t2_sampler sm;
    t2_result r; uint32_t gen, flt; int64_t composed;
    uint32_t nan_bits=0x7fc00000u;

    memset(f,0,sizeof(f)); memset(out,0,sizeof(out));
    memset(&sample,0,sizeof(sample)); memset(&rd,0,sizeof(rd));
    for(i=0;i<16;i++){ boot[i]=(uint8_t)(i+1); boot2[i]=(uint8_t)(i+17); }
    memcpy(build,"T2-DEMO-BUILD-01",16);
    checks_buf[0]='\0'; vectors_buf[0]='\0';

    /* 1. CRC bit-exact with the board's table-driven checksum_crc8. */
    { int ok=1; uint8_t probe[1];
      for(i=0;i<256;i++){ probe[0]=(uint8_t)i; if(t2_crc8(probe,1)!=t2_board_crc8_table[i]) ok=0; }
      { const uint8_t *str=(const uint8_t*)"123456789";
        if(t2_crc8(str,9)!=0xA1u || t2_board_crc8(str,9)!=0xA1u) ok=0; }
      check("crc8_matches_board_table_all_256_and_check_value",ok); }

    /* 2. fail-closed init. */
    memset(&s,0,sizeof(s)); memset(f,0xaa,sizeof(f));
    check("zero_boot_id_rejected",!t2_init(&s,zero));
    check("no_capabilities_without_valid_boot",t2_capabilities(&s,build,out,sizeof(out))==0);
    check("no_snapshot_without_valid_boot",t2_snapshot(&s,&sample,out,sizeof(out))==0);
    check("command_rejected_when_boot_invalid",t2_command_ex(&s,f,10,0)==T2_REJ_DISABLED_BOOT);

    /* 3. golden vectors produced by the ACTUAL C serializers/command parser. */
    check("init_with_real_boot",t2_init(&s,boot)==1);
    n=t2_capabilities(&s,build,out,sizeof(out));
    check("c_capabilities_len_43",n==43);
    vector("capabilities",out,n);

    n=make_command(f,boot,1,7,1,100,zeros);
    check("golden_enable_accepted",t2_command_ex(&s,f,n,0)==T2_OK);
    vector("enable",f,n);
    n=make_command(f,boot,2,7,2,100,speeds);
    check("golden_update_accepted",t2_command_ex(&s,f,n,1)==T2_OK);
    vector("update",f,n);
    check("deadline_is_101_after_update",s.deadline_ms==101);

    for(i=0;i<4;i++){ sample.count[i]=c1[i];
                       sample.measured_rps[i]=(float)(((int)i)-1)*0.5f;
                       sample.target_rps[i]=(float)i*0.25f; }
    sample.tick_ms=1000; sample.sample_seq=1; sample.faults=0;
    n=t2_snapshot(&s,&sample,out,sizeof(out));
    check("c_snapshot_len_98",n==98);
    check("c_snapshot_count_offset_33",out[33]==0x5f && out[34]==0xea); /* 59999 = 0xEA5F */
    vector("snapshot",out,n);

    for(i=0;i<4;i++) sample.count[i]=c2[i];
    sample.tick_ms=1010; sample.sample_seq=2;
    n=t2_snapshot(&s,&sample,out,sizeof(out));
    vector("count_wrap",out,n);
    check("count_wrap_delta_is_2",c2[0]-c1[0]==2);

    n=make_snapshot(f,boot2,10,1,0,c3); vector("new_boot",f,n);
    n=make_snapshot(f,boot,1000,1,0,c1); f[n-1]^=1u; vector("bad_crc",f,n);
    n=make_snapshot(f,boot,1000,1,0,c1); f[2]=3; reseal(f); vector("wrong_function",f,n);
    n=make_snapshot(f,boot,1000,1,0,c1); vector("wrong_length",f,n-1);
    qlen=make_frame(f,T2_FUNCTION,(const uint8_t*)"\x01\x01",2); vector("capability_query",f,qlen);
    check("capability_query_len_7",qlen==7);
    check("capability_query_sub_and_version",f[4]==0x01 && f[5]==0x01);
    n=make_command(f,boot,3,7,3,0,zeros); vector("stop",f,n);
    n=make_command(f,boot,4,8,1,0,zeros); vector("release",f,n);
    vector_empty("legacy_no_response");

    /* 4. invalid frames / wrong source / wrong boot / length / CRC / values. */
    n=make_command(f,boot,2,7,2,100,speeds);
    check("replay_same_seq_rejected",t2_command_ex(&s,f,n,2)==T2_REJ_SEQ);
    check("replay_did_not_change_target",s.target[1]==0.25f);
    check("replay_did_not_refresh_deadline",s.deadline_ms==101);
    { uint8_t other[16]; memset(other,0x5a,16);
      n=make_command(f,other,2,7,3,100,speeds);
      check("wrong_boot_source_rejected",t2_command_ex(&s,f,n,2)==T2_REJ_BOOT); }
    n=make_command(f,boot,2,7,3,100,speeds); f[37]=4; reseal(f);
    check("illegal_motor_id_rejected",t2_command_ex(&s,f,n,2)==T2_REJ_MOTOR_ID);
    n=make_command(f,boot,2,7,3,100,speeds); f[42]=0; reseal(f);
    check("duplicate_motor_id_rejected",t2_command_ex(&s,f,n,2)==T2_REJ_DUP_ID);
    n=make_command(f,boot,2,7,3,100,speeds); wr32(f+38,0x7fc00000u); reseal(f);
    check("nan_target_rejected",t2_command_ex(&s,f,n,2)==T2_REJ_NAN_INF);
    n=make_command(f,boot,2,7,3,100,speeds); wr32(f+38,0x7f800000u); reseal(f);
    check("inf_target_rejected",t2_command_ex(&s,f,n,2)==T2_REJ_NAN_INF);
    large[0]=7.0f; n=make_command(f,boot,2,7,3,100,large);
    check("over_range_rps_rejected",t2_command_ex(&s,f,n,2)==T2_REJ_SPEED_RANGE);
    n=make_command(f,boot,2,7,3,100,speeds); f[2]=3; reseal(f);
    check("wrong_function_rejected",t2_command_ex(&s,f,n,2)==T2_REJ_FUNCTION);
    n=make_command(f,boot,2,7,3,100,speeds); f[36]=3; reseal(f);
    check("wrong_count_rejected",t2_command_ex(&s,f,n,2)==T2_REJ_COUNT);
    n=make_command(f,boot,2,7,3,0,speeds);
    check("zero_ttl_rejected",t2_command_ex(&s,f,n,2)==T2_REJ_TTL);
    n=make_command(f,boot,2,7,3,501,speeds);
    check("oversize_ttl_rejected",t2_command_ex(&s,f,n,2)==T2_REJ_TTL);
    n=make_command(f,boot,2,7,3,100,speeds);
    check("short_frame_rejected",t2_command_ex(&s,f,n-1,2)==T2_REJ_LENGTH);
    f[n-1]^=1u;
    check("bad_crc_rejected",t2_command_ex(&s,f,n,2)==T2_REJ_CRC);
    n=make_command(f,boot,2,6,4,100,speeds);
    check("old_epoch_rejected",t2_command_ex(&s,f,n,2)==T2_REJ_EPOCH_MISMATCH);
    n=make_command(f,boot,1,7,9,100,zeros);
    check("enable_while_enabled_rejected",t2_command_ex(&s,f,n,2)==T2_REJ_ALREADY_ENABLED);
    { float nz[4]={0,0,0,0}; nz[0]=0.1f;
      n=make_command(f,boot,1,8,1,100,nz);
      check("enable_with_nonzero_speed_rejected",t2_command_ex(&s,f,n,2)==T2_REJ_ENABLE_NONZERO); }
    check("rejections_are_latched",(s.latched_faults&T2_FAULT_LATCH_REJECT)!=0);
    check("rejections_did_not_enable_or_move",s.enabled==1 && s.target[1]==0.25f);
    r=t2_last_reject(&s);
    check("last_reject_is_named",strcmp(t2_result_name(r),"REJ_ENABLE_NONZERO")==0);

    /* 5. expiry, latch, re-enable, cancel, release, legacy gate. */
    t2_expire(&s,101);
    check("expiry_zeros_all_targets",!s.enabled && s.target[0]==0.0f && s.target[1]==0.0f &&
                                      s.target[2]==0.0f && s.target[3]==0.0f);
    check("expiry_latches_timeout",(s.faults&T2_FAULT_TIMEOUT)!=0 &&
                                   (s.latched_faults&T2_FAULT_LATCH_TIMEOUT)!=0);
    n=make_command(f,boot,2,7,10,100,speeds);
    check("drive_after_expiry_rejected",t2_command_ex(&s,f,n,102)==T2_REJ_NOT_ENABLED);
    check("legacy_motor_path_still_gated_after_expiry",t2_gate_legacy(&s)==1);
    n=make_command(f,boot,1,7,1,100,zeros);
    check("old_epoch_enable_after_expiry_rejected",t2_command_ex(&s,f,n,102)==T2_REJ_EPOCH_OLD);
    n=make_command(f,boot,1,8,1,100,zeros);
    check("fresh_epoch_enable_accepted",t2_command_ex(&s,f,n,102)==T2_OK);
    check("timeout_latch_survives_re_enable",(s.latched_faults&T2_FAULT_LATCH_TIMEOUT)!=0);
    check("current_faults_cleared_on_enable",s.faults==0);
    n=make_command(f,boot,3,8,2,0,zeros);
    check("cancel_accepted",t2_command_ex(&s,f,n,103)==T2_OK);
    check("cancel_disables_and_keeps_gate",!s.enabled && t2_gate_legacy(&s)==1);
    n=make_command(f,boot,4,8,3,0,zeros);
    check("release_with_old_epoch_rejected",t2_command_ex(&s,f,n,104)==T2_REJ_EPOCH_OLD);
    n=make_command(f,boot,4,9,1,0,zeros);
    check("release_accepted",t2_command_ex(&s,f,n,104)==T2_OK);
    check("release_clears_latch_and_gate",s.latched_faults==0 && t2_gate_legacy(&s)==0);
    check("release_does_not_enable",!s.enabled);
    { float ones[4]={1,1,1,1};
      n=make_command(f,boot,1,10,1,100,zeros);
      check("enable_after_release_accepted",t2_command_ex(&s,f,n,105)==T2_OK);
      n=make_command(f,boot,2,10,2,100,ones);
      check("drive_after_release_accepted",t2_command_ex(&s,f,n,106)==T2_OK);
      t2_force_stop(&s);
      check("force_stop_zeros_and_latches_estop",!s.enabled && s.target[0]==0.0f &&
            (s.latched_faults&T2_FAULT_LATCH_ESTOP)!=0); }

    /* 6. 32-bit monotonic wrap. */
    memset(&sb,0,sizeof(sb)); check("wrap_state_init",t2_init(&sb,boot)==1);
    n=make_command(f,boot,1,1,1,100,zeros);
    check("wrap_enable_at_fffffff0",t2_command_ex(&sb,f,n,0xfffffff0u)==T2_OK);
    t2_expire(&sb,83); check("wrap_no_early_expiry",sb.enabled==1);
    t2_expire(&sb,84); check("wrap_expiry_at_rollover",sb.enabled==0);

    /* 7. encoder overflow composition, torn read, snapshot consistency. */
    check("compose_59999",t2_compose_count_checked(0,59999,0,T2_TICKS_OVERFLOW,&composed)==1 && composed==59999);
    check("compose_after_wrap_60001",t2_compose_count_checked(1,0,1,T2_TICKS_OVERFLOW,&composed)==1 && composed==60001);
    check("compose_rejects_torn_overflow",t2_compose_count_checked(1,0,2,T2_TICKS_OVERFLOW,&composed)==0);
    check("compose_rejects_counter_above_arr",t2_compose_count_checked(0,60001,0,T2_TICKS_OVERFLOW,&composed)==0);
    check("compose_negative_direction",t2_compose_count_checked(-1,2,-1,T2_TICKS_OVERFLOW,&composed)==1 && composed==-59999);

    check("sample_is_finite",t2_sample_is_finite(&sample)==1);
    bad=sample; memcpy(&bad.measured_rps[2],&nan_bits,4);
    check("sample_nan_is_not_finite",t2_sample_is_finite(&bad)==0);
    check("snapshot_refuses_non_finite_sample",t2_snapshot(&s,&bad,out,sizeof(out))==0);

    for(i=0;i<4;i++) sample.count[i]=c1[i];
    t2_sampler_init(&sm);
    check("sampler_initial_generation_even",(sm.seq&1u)==0);
    t2_sampler_begin(&sm,1);
    check("sampler_read_rejects_writer_active",t2_sampler_read(&sm,&rd)==0);
    sm.sample=sample; t2_sampler_commit(&sm);
    check("sampler_read_after_commit",t2_sampler_read(&sm,&rd)==1 && rd.count[0]==59999);
    check("sampler_read_begin_ok",t2_sampler_read_begin(&sm,&rd,&gen)==1 && !(gen&1u));
    t2_sampler_publish(&sm,&sample);  /* ISR publishes while reader is mid-flight */
    check("sampler_read_end_detects_torn_snapshot",t2_sampler_read_end(&sm,gen)==0);
    check("sampler_read_after_new_generation",t2_sampler_read(&sm,&rd)==1);
    check("sampler_publish_copies_all_four_counts",
          rd.count[0]==59999 && rd.count[1]==-1 && rd.count[2]==0 && rd.count[3]==123);

    /* 8. output bounds and latched fault visible on the wire after a timeout. */
    check("snapshot_cap_too_small_returns_zero",t2_snapshot(&s,&sample,out,97)==0);
    check("capabilities_cap_too_small_returns_zero",t2_capabilities(&s,build,out,42)==0);
    { uint8_t frame2[128]; size_t m;
      memset(&sb,0,sizeof(sb)); t2_init(&sb,boot);
      n=make_command(f,boot,1,1,1,100,zeros); t2_command_ex(&sb,f,n,0);
      t2_expire(&sb,100);
      m=t2_snapshot(&sb,&sample,frame2,sizeof(frame2));
      flt=(uint32_t)frame2[29]|(uint32_t)frame2[30]<<8|
          (uint32_t)frame2[31]<<16|(uint32_t)frame2[32]<<24;
      check("latched_timeout_present_in_wire_faults",m==98 && (flt&T2_FAULT_LATCH_TIMEOUT)!=0); }
    check("query_buffer_overflow_safe",t2_capabilities_query(q,6)==0 &&
                                        t2_capabilities_query(out,sizeof(out))==7);

    printf("{\n  \"tool\":\"t2_native_host_harness\",\n"
           "  \"kind\":\"compiled_C_host_logic_not_MCU_build\",\n"
           "  \"checks\":[\n%s\n  ],\n  \"vectors\":{\n%s\n  },\n"
           "  \"total\":%d,\n  \"failed\":%d,\n  \"result\":\"%s\"\n}\n",
           checks_buf,vectors_buf,g_total,g_failed,g_failed?"FAIL":"PASS");
    return g_failed?1:0;
}
