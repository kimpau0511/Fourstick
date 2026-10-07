"""FR3-WMS 관절 한계 시험 (PC1용, gz_ros2_control + arm_trajectory_controller 경로).

팔 단독 Gazebo(run_gazebo_fr3.sh, 도메인 42)에 붙어서 관절 하나씩
  1) 한계 + 0.5 rad 목표를, 속도 한계의 4배가 필요한 시간으로 보낸다
  2) 도달 위치와 /joint_states 최고 속도를 잰다
  3) 0 자세로 되돌린다
를 위·아래 양쪽으로 반복하고 표로 찍는다. 한계 값은 공식 URDF에서 읽는다.

사용: python3 joint_limit_probe.py <FR3WMS.urdf 경로>
"""
import sys
import time
import xml.etree.ElementTree as ET

import rclpy
from builtin_interfaces.msg import Duration
from control_msgs.action import FollowJointTrajectory
from rclpy.action import ActionClient
from rclpy.node import Node
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectoryPoint

JOINTS = ['j1', 'j2', 'j3', 'j4', 'j5', 'j6']
ACTION = '/arm_trajectory_controller/follow_joint_trajectory'
OVER = 0.5          # 한계 밖으로 더 보내는 양(rad)
SPEED_X = 4.0       # 속도 한계의 몇 배로 명령할지
POS_TOL = 0.01      # 이만큼 넘으면 '넘음'(rad)
SHORT_TOL = 0.05    # 한계보다 이만큼 못 미치면 '한계 전 멈춤'(rad)
VEL_TOL = 1.05      # 속도 한계의 5% 초과면 '속도 넘음'


def read_limits(urdf):
    out = {}
    for j in ET.parse(urdf).getroot().findall('joint'):
        if j.get('name') in JOINTS:
            lim = j.find('limit')
            out[j.get('name')] = (float(lim.get('lower')), float(lim.get('upper')), float(lim.get('velocity')))
    missing = [n for n in JOINTS if n not in out]
    if missing:
        sys.exit(f'URDF에 관절이 없다: {missing}')
    return out


class Probe(Node):
    def __init__(self):
        super().__init__('forstick_joint_limit_probe')
        self.pos, self.vel, self.stamp = {}, {}, None
        self.create_subscription(JointState, '/joint_states', self.on_state, 100)
        self.client = ActionClient(self, FollowJointTrajectory, ACTION)

    def on_state(self, msg):
        for n, p, v in zip(msg.name, msg.position, msg.velocity or [0.0] * len(msg.name)):
            self.pos[n], self.vel[n] = p, v
        self.stamp = time.monotonic()

    def spin_for(self, sec, watch=None, peak=None):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            rclpy.spin_once(self, timeout_sec=0.01)
            if watch and peak is not None and watch in self.vel:
                peak[0] = max(peak[0], abs(self.vel[watch]))

    def send(self, targets, seconds, watch=None):
        """목표를 보내고 결과까지 기다린다. (accepted, error_code, peak_vel)"""
        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = JOINTS
        pt = JointTrajectoryPoint()
        pt.positions = [float(targets[n]) for n in JOINTS]
        pt.velocities = [0.0] * len(JOINTS)
        pt.time_from_start = Duration(sec=int(seconds), nanosec=int((seconds % 1) * 1e9))
        goal.trajectory.points = [pt]
        peak = [0.0]
        fut = self.client.send_goal_async(goal)
        while not fut.done():
            self.spin_for(0.01, watch, peak)
        handle = fut.result()
        if not handle.accepted:
            return False, None, peak[0]
        res = handle.get_result_async()
        deadline = time.monotonic() + seconds + 20
        while not res.done() and time.monotonic() < deadline:
            self.spin_for(0.01, watch, peak)
        self.spin_for(1.0, watch, peak)   # 결과 뒤 1초 더 관측(넘어가는지)
        code = res.result().result.error_code if res.done() else 'timeout'
        return True, code, peak[0]


def main():
    limits = read_limits(sys.argv[1])
    rclpy.init()
    node = Probe()
    if not node.client.wait_for_server(timeout_sec=30):
        sys.exit(f'{ACTION} 액션 서버가 없다 — 팔 단독 Gazebo가 떠 있는지 확인')
    node.spin_for(2.0)
    if node.stamp is None:
        sys.exit('/joint_states를 받지 못했다')
    zero = {n: 0.0 for n in JOINTS}
    node.send(zero, 5.0)

    rows, fails = [], 0
    for n in JOINTS:
        lo, hi, vmax = limits[n]
        for side, limit in (('위', hi), ('아래', lo)):
            target = limit + OVER if side == '위' else limit - OVER
            seconds = max(abs(target) / (vmax * SPEED_X), 0.2)
            ok, code, peak = node.send({**zero, n: target}, seconds, watch=n)
            reached = node.pos.get(n, float('nan'))
            excess = (reached - hi) if side == '위' else (lo - reached)
            if not ok:
                verdict = '컨트롤러가 목표 거부(막힘)'
            elif excess > POS_TOL:
                verdict = '한계 넘음 ✗'
            elif excess < -SHORT_TOL:
                verdict = '한계 전 멈춤(바닥·충돌 등, 판정 불가)'
            else:
                verdict = '한계에서 멈춤 ✓'
            vel_note = '속도 넘음 ✗' if peak > vmax * VEL_TOL else '속도 OK'
            if '✗' in verdict or '✗' in vel_note:
                fails += 1
            rows.append((n, side, limit, target, reached, excess, peak, vmax, code, verdict, vel_note))
            node.send(zero, max(abs(reached) / vmax * 2, 2.0) if reached == reached else 10.0)

    print(f'\n관절 한계 시험 (한계 ±{OVER} rad, 속도 한계 x{SPEED_X}로 명령)')
    print(f'{"관절":4} {"쪽":3} {"한계":>8} {"명령":>8} {"도달":>8} {"넘은양":>8} {"최고속도":>8} {"속도한계":>8} {"결과코드":>8}  판정')
    for n, side, limit, target, reached, excess, peak, vmax, code, verdict, vel_note in rows:
        print(f'{n:4} {side:3} {limit:8.4f} {target:8.4f} {reached:8.4f} {excess:8.4f} {peak:8.3f} {vmax:8.2f} {str(code):>8}  {verdict} / {vel_note}')
    print(f'\n넘은 항목: {fails}개')
    node.destroy_node()
    rclpy.shutdown()
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
