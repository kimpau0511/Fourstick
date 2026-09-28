"""Unitree G1(12자유도 다리) Gazebo 시뮬레이션 — 웹 연결용 다리(`bridge.G1Bridge`).

제어는 `humanoid/g1/nav_controller.py`(별도 프로세스)가 맡는다. 이 패키지는 그 제어기에
명령을 보내고 상태·관측을 읽는다. 실제 로봇이 아니다(is_simulated).
"""
