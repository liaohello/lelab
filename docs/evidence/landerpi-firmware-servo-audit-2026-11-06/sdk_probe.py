"""Run only the vendor torque method AST. Never import/construct the serial SDK."""
import ast
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

SDK = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
    '/home/liaozhengyan/Desktop/resource/lander_pi/src/driver/ros_robot_controller/'
    'ros_robot_controller/ros_robot_controller_sdk.py')
tree = ast.parse(SDK.read_text())
method = next(node for node in ast.walk(tree)
              if isinstance(node, ast.FunctionDef) and node.name == 'bus_servo_enable_torque')
namespace = {'struct': struct, 'time': SimpleNamespace(sleep=lambda _: None),
             'PacketFunction': SimpleNamespace(PACKET_FUNC_BUS_SERVO=5)}
exec(compile(ast.Module(body=[method], type_ignores=[]), str(SDK), 'exec'), namespace)
rows = []
for enabled in (True, False):
    def capture(function, payload):
        rows.append({'enable_argument': enabled, 'function': function,
                     'payload_hex': payload.hex(' '),
                     'firmware_action': 'unload' if payload[0] == 11 else
                                       ('load' if payload[0] == 12 else 'unknown')})
    namespace['bus_servo_enable_torque'](SimpleNamespace(buf_write=capture), 1, enabled)
print(json.dumps({'scope': 'offline AST extraction; no serial constructed',
                  'source': str(SDK), 'rows': rows}, indent=2))
