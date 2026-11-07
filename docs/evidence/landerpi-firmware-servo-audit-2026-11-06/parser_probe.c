/* OFFLINE ONLY: exercises the unmodified vendor parser header; no HAL or hardware. */
#include <stdio.h>
#include <string.h>
#include "serial_servo.h"

static int feed(unsigned id, unsigned cmd, const unsigned char *args, unsigned n) {
    SerialServoControllerTypeDef c;
    unsigned char wire[16] = {0x55, 0x55, 0, 0, 0};
    unsigned sum = 0;
    int accepted = 0;
    memset(&c, 0, sizeof(c));
    wire[2] = (unsigned char)id;
    wire[3] = (unsigned char)(n + 3);
    wire[4] = (unsigned char)cmd;
    memcpy(wire + 5, args, n);
    for (unsigned i = 2; i < n + 5; ++i) sum += wire[i];
    wire[n + 5] = (unsigned char)~sum;
    for (unsigned i = 0; i < n + 6; ++i)
        if (serial_servo_rx_handler(&c, wire[i]) == 0) ++accepted;
    return accepted;
}

int main(void) {
    const unsigned char args[] = {11, 22, 33, 44};
    const unsigned char malformed[] = {0x55, 0x55, 1, 2, 28};
    SerialServoControllerTypeDef c;
    int torque = feed(1, 32, args, 1);
    int position = feed(1, 28, args, 2);
    int no_args = feed(1, 14, args, 0);
    int three_args = feed(1, 23, args, 3);
    int four_args = feed(1, 23, args, 4);
    /* The parser is not given the requested ID/command anywhere. */
    int unrelated = feed(2, 32, args, 1);
    memset(&c, 0, sizeof(c));
    for (unsigned i = 0; i < sizeof(malformed); ++i)
        (void)serial_servo_rx_handler(&c, malformed[i]);
    /* Stop before the ninth byte would write past args[8]. */
    for (unsigned i = 0; i < 8; ++i) (void)serial_servo_rx_handler(&c, 0);
    int exhausted = c.rx_state == SERIAL_SERVO_RECV_ARGUMENTS && c.rx_args_index == 8;
    printf("{\"torque_frame_accepted\":%d,\"position_frame_accepted\":%d,"
           "\"zero_argument_frame_accepted\":%d,\"three_argument_frame_accepted\":%d,"
           "\"four_argument_frame_accepted\":%d,\"unrelated_frame_accepted\":%d,"
           "\"malformed_length_exhausted_argument_buffer\":%d}\n",
           torque, position, no_args, three_args, four_args, unrelated, exhausted);
    /* Exit zero means the reported vendor defects were reproduced, not fixed. */
    return !(torque == 1 && position == 1 && no_args == 0 && three_args == 0
             && four_args == 1 && unrelated == 1 && exhausted == 1);
}
