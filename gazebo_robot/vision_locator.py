"""scene_camera(/scene_camera/image, sensor_msgs/Image rgb8)를 색상 기반으로
분석해서 A/B/C자재의 world-frame (x, y)를 추정하는 간이 비전 모듈.

로봇은 지금까지 robot_config.RobotConfig.location_poses에 박아둔 좌표만
믿고 그 자리로 내려가는 완전 오픈루프 방식이었다(taskplan_bridge.py의
exec_pick 참고) — 물체가 스폰 위치에서 조금이라도 밀려나면(실제로 pick
실패 재시도 도중 다른 물체가 밀려나는 사고가 실측으로 확인됨) 로봇은 그걸
전혀 모른 채 원래 좌표로 내려가 허공을 집는다. 이 모듈은 그 대신 카메라로
물체의 실제 현재 위치를 추정해서 pick 좌표를 보정하는 데 쓴다.

캘리브레이션 방법 (2026-09-08): pallets_world.sdf의 scene_camera 센서 스펙
(horizontal_fov=1.2rad, 960x540, pose="0.1 -1.0 0.9 0 0.35 1.5708")으로
핀홀 카메라 모델을 analytic하게 세우고, 실제 캡처한 프레임에서 material_a/b
색상 블롭 중심 픽셀과 gz topic으로 읽은 실제 world 좌표를 대조해서 검증함.
물체가 정육면체(한 변 0.035m)라 카메라가 보는 건 물체 중심이 아니라 윗면
(카메라가 위에서 내려다보므로)이라, 보정 없이 "물체는 z=grasp_z 평면에
있다"고 가정하고 광선-평면 교차를 풀면 물체 절반 높이만큼 오차가 생김
(실측 오차 약 3.1~3.4cm) — z_plane을 grasp_z + OBJECT_HALF_HEIGHT_M(윗면
높이)로 보정하니 오차가 1~4mm로 줄어듦(실측). 이 보정을 그대로 반영함.
"""
import cv2
import numpy as np

# ============================================================
# scene_camera 스펙 (pallets_world.sdf 그대로)
# ============================================================
IMAGE_WIDTH = 960
IMAGE_HEIGHT = 540
HORIZONTAL_FOV = 1.2  # rad
CAMERA_POS = np.array([0.1, -1.0, 0.9])
CAMERA_RPY = (0.0, 0.35, 1.5708)  # roll, pitch, yaw (SDF fixed-axis)

# 물체(material_a/b/c) 공통 크기 — pallets_world.sdf <box><size>0.035 ...
OBJECT_HALF_HEIGHT_M = 0.035 / 2

# pallets_world.sdf의 <material><ambient>/<diffuse> 값(0~1) * 255.
# Task Plan 물체명(pipeline.py CAPABILITY_PROFILE) -> (모델명, RGB) 매핑.
OBJECT_COLORS = {
    "A자재": ("material_a", (217, 51, 51)),   # 0.85 0.2 0.2
    "B자재": ("material_b", (51, 191, 76)),   # 0.2 0.75 0.3
    "C자재": ("material_c", (64, 89, 217)),   # 0.25 0.35 0.85
}

# 색상 매칭 허용 오차(RGB 채널당). 조명/그림자로 값이 좀 흔들려도 잡히게
# 넉넉히 잡되, 배경(회색 바닥/팔레트 갈색)과는 안 겹치는 값으로 실측 확인함.
COLOR_TOLERANCE = 60
# 이 픽셀 수 미만이면(물체가 화면 구석에 살짝 걸치거나 팔에 가려 일부만
# 보이는 경우 등) 중심 추정이 부정확할 수 있어 "못 찾음"으로 처리.
MIN_BLOB_PIXELS = 20


def _rotation_x(a):
    c, s = np.cos(a), np.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def _rotation_y(a):
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def _rotation_z(a):
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def _camera_intrinsics():
    fx = IMAGE_WIDTH / (2 * np.tan(HORIZONTAL_FOV / 2))
    fy = fx  # 정사각 픽셀 가정 (Gazebo 기본)
    cx, cy = IMAGE_WIDTH / 2, IMAGE_HEIGHT / 2
    return fx, fy, cx, cy


def _camera_extrinsics():
    roll, pitch, yaw = CAMERA_RPY
    # SDF fixed-axis(extrinsic) 컨벤션: R = Rz(yaw) @ Ry(pitch) @ Rx(roll)
    r_body_to_world = _rotation_z(yaw) @ _rotation_y(pitch) @ _rotation_x(roll)
    # body(REP103: x-forward,y-left,z-up) -> optical(x-right,y-down,z-forward)
    r_opt_from_body = np.array([
        [0, -1, 0],
        [0, 0, -1],
        [1, 0, 0],
    ])
    return r_body_to_world, r_opt_from_body


_FX, _FY, _CX, _CY = _camera_intrinsics()
_R_BODY_TO_WORLD, _R_OPT_FROM_BODY = _camera_extrinsics()


def find_blob_centroid(image_rgb, target_rgb, tol=COLOR_TOLERANCE, min_pixels=MIN_BLOB_PIXELS):
    """image_rgb: HxWx3 uint8 배열(rgb8). target_rgb 색상에 가까운 픽셀들 중
    가장 큰 연결 성분(connected component)의 중심 픽셀 좌표(u, v)를 반환.
    못 찾으면 None.

    [2026-09-08] 처음엔 threshold를 통과한 "모든" 픽셀의 단순 평균을 썼는데,
    실측으로 잘못된 위치가 나온 사고가 있었음(하드코딩(0.4,0.3) 대비
    실측(0.456,0.567)로 26cm나 틀림, DetachableJoint가 그 틀린 위치에서
    그냥 attach를 걸어버려 물체가 허공에 붙어 딸려가는 사고로 이어짐).
    원인으로 의심되는 건 바닥/팔레트의 그림자·조명 그라디언트가 tolerance
    안에 우연히 걸리는 산발적 픽셀들이 평균을 크게 끌고 가는 것 — 연결
    성분 중 가장 큰 것만 쓰면(실제 물체는 그 자체로 조밀한 사각형 덩어리)
    이런 산발적 오탐에 훨씬 덜 흔들린다."""
    diff = np.abs(image_rgb.astype(np.int16) - np.array(target_rgb, dtype=np.int16))
    mask = np.all(diff < tol, axis=-1).astype(np.uint8)
    num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if num_labels <= 1:  # 배경(0)만 있음
        return None
    # label 0은 배경 — 1부터 순회하며 가장 큰 연결 성분을 고른다.
    best_label = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if stats[best_label, cv2.CC_STAT_AREA] < min_pixels:
        return None
    return float(centroids[best_label][0]), float(centroids[best_label][1])


def pixel_to_world_xy(u, v, z_plane):
    """픽셀(u, v)을 지나는 카메라 광선과 world z=z_plane 평면의 교점(x, y).
    카메라 뒤쪽/평행이라 교점이 없으면 None."""
    dir_opt = np.array([(u - _CX) / _FX, (v - _CY) / _FY, 1.0])
    dir_body = _R_OPT_FROM_BODY.T @ dir_opt
    dir_world = _R_BODY_TO_WORLD @ dir_body
    if abs(dir_world[2]) < 1e-9:
        return None
    t = (z_plane - CAMERA_POS[2]) / dir_world[2]
    if t <= 0:
        return None
    point = CAMERA_POS + t * dir_world
    return float(point[0]), float(point[1])


def locate_object(image_rgb, object_name, resting_z):
    """image_rgb(HxWx3 uint8, rgb8) 안에서 object_name(예: "A자재")을 색상으로
    찾아 world (x, y)를 추정. resting_z는 물체가 놓인 표면의 z(예: 목표
    위치의 grasp_z) — 물체가 그 표면에 얹혀 있다는 가정 하에, 카메라가 보는
    윗면 높이(resting_z + OBJECT_HALF_HEIGHT_M)로 보정해서 광선-평면 교차를
    푼다. 색상을 못 찾거나 매핑이 없으면 None(호출부는 하드코딩 좌표로
    폴백해야 함)."""
    entry = OBJECT_COLORS.get(object_name)
    if entry is None:
        return None
    _model_name, rgb = entry
    centroid = find_blob_centroid(image_rgb, rgb)
    if centroid is None:
        return None
    z_top = resting_z + OBJECT_HALF_HEIGHT_M
    return pixel_to_world_xy(centroid[0], centroid[1], z_top)
