#!/usr/bin/env python3
"""현재 FR3 Gazebo 작업셀을 기존 PostgreSQL public 카탈로그와 동기화한다.

기존 CELL-01, 팔레트·컨베이어·자재·2F-85 도구를 재사용한다. 실행할 때마다
같은 결과가 되며, 미확보된 payload 같은 값을 만들어 넣지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config import ServerConfig  # noqa: E402


RESOURCE_CODES = {
    "loc_pallet_1": ("PAL-01", "pallet_1_frame", "pallet_1"),
    "loc_pallet_2": ("PAL-02", "pallet_2_frame", "pallet_2"),
    "loc_pallet_3": ("PAL-03", "pallet_3_frame", "pallet_3"),
    "loc_conveyor": ("CONV-01", "conveyor_frame", "conveyor"),
}
MATERIAL_CODES = {"mat_a": "MAT-A", "mat_b": "MAT-B", "mat_c": "MAT-C"}


def main() -> int:
    try:
        import psycopg2
        from psycopg2.extras import Json
    except ImportError:
        print("psycopg2-binary가 필요합니다: pip install -r requirements-db.txt",
              file=sys.stderr)
        return 2

    config = ServerConfig.from_env()
    if not config.db_password:
        print("FORSTICK2_DB_PASSWORD가 필요합니다", file=sys.stderr)
        return 2

    workcell = json.loads((
        ROOT / "config/workcell/fr3_2f85_workcell.json"
    ).read_text(encoding="utf-8"))
    resources = json.loads((
        ROOT / "config/workcell/fr3_2f85_workcell_resource_catalog.json"
    ).read_text(encoding="utf-8"))
    capability_path = ROOT / "config/profiles/fr3wms_2f85_workcell_capability.json"
    capability = json.loads(capability_path.read_text(encoding="utf-8"))
    aliases = {row["resource_id"]: row.get("aliases", [])
               for row in resources["entries"]}

    connection = psycopg2.connect(
        host=config.db_host, port=config.db_port, dbname=config.db_name,
        user=config.db_user, password=config.db_password, connect_timeout=8,
        application_name="fourstick-catalog-sync",
    )
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT robot_type_id FROM public.robot_types "
                "WHERE type_code = 'COBOT_ARM'"
            )
            robot_type_id = cursor.fetchone()[0]
            cursor.execute(
                "SELECT work_cell_id FROM public.work_cells "
                "WHERE cell_code = 'CELL-01'"
            )
            work_cell_id = cursor.fetchone()[0]

            cursor.execute(
                "SELECT robot_id FROM public.robots WHERE robot_code = 'FR3-SIM-01'"
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    INSERT INTO public.robots
                      (robot_type_id, robot_code, model_name, manufacturer,
                       current_cell_id, status_code, is_enabled)
                    VALUES (%s, 'FR3-SIM-01', 'FR3WMS + Robotiq 2F-85',
                            'FAIR Innovation', %s, 'OFFLINE', true)
                    RETURNING robot_id
                    """,
                    (robot_type_id, work_cell_id),
                )
                robot_id = cursor.fetchone()[0]
            else:
                robot_id = row[0]
                cursor.execute(
                    """
                    UPDATE public.robots
                    SET model_name = 'FR3WMS + Robotiq 2F-85',
                        manufacturer = 'FAIR Innovation', current_cell_id = %s,
                        is_enabled = true, updated_at = now()
                    WHERE robot_id = %s
                    """,
                    (work_cell_id, robot_id),
                )

            for skill_code in capability["supported_skills"]:
                cursor.execute(
                    "SELECT skill_id FROM public.skills WHERE skill_code = %s",
                    (skill_code,),
                )
                skill = cursor.fetchone()
                if skill is not None:
                    cursor.execute(
                        """
                        INSERT INTO public.robot_skills
                          (robot_id, skill_id, enabled, config_json)
                        VALUES (%s, %s, true, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (robot_id, skill[0], Json({"source": str(
                            capability_path.relative_to(ROOT))})),
                    )

            cursor.execute(
                "SELECT tool_id FROM public.tools WHERE tool_code = 'GRIPPER-2F85'"
            )
            tool = cursor.fetchone()
            if tool is not None:
                cursor.execute(
                    """
                    SELECT 1 FROM public.robot_tool_assignments
                    WHERE robot_id = %s AND tool_id = %s AND ended_at IS NULL
                    """,
                    (robot_id, tool[0]),
                )
                if cursor.fetchone() is None:
                    cursor.execute(
                        """
                        INSERT INTO public.robot_tool_assignments (robot_id, tool_id)
                        VALUES (%s, %s)
                        """,
                        (robot_id, tool[0]),
                    )

            for runtime_id, (code, frame_name, model_name) in RESOURCE_CODES.items():
                frame = workcell["frames"][frame_name]
                model = workcell["models"][model_name]
                pose = {
                    "frame": frame["parent"], "xyz_m": frame["xyz_m"],
                    "rpy_rad": frame["rpy_rad"],
                }
                geometry = {
                    key: model[key] for key in ("kind", "size_m", "parts")
                    if key in model
                }
                metadata = {
                    "runtime_resource_id": runtime_id,
                    "workcell_id": workcell["workcell_id"],
                    "workcell_version": workcell["workcell_version"],
                    "source": "config/workcell/fr3_2f85_workcell.json",
                    "is_simulated": True,
                }
                cursor.execute(
                    """
                    UPDATE public.cell_resources
                    SET pose_json = %s, geometry_json = %s,
                        aliases_json = %s,
                        metadata_json = metadata_json || %s,
                        updated_at = now()
                    WHERE resource_code = %s
                    """,
                    (Json(pose), Json(geometry), Json(aliases[runtime_id]),
                     Json(metadata), code),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"기존 작업셀 자원이 없다: {code}")

            for runtime_id, material_code in MATERIAL_CODES.items():
                cursor.execute(
                    """
                    UPDATE public.materials
                    SET aliases_json = %s, updated_at = now()
                    WHERE material_code = %s
                    """,
                    (Json(aliases[runtime_id]), material_code),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"기존 자재가 없다: {material_code}")

            checksum = hashlib.sha256(
                capability_path.read_bytes()
            ).hexdigest()
            print(json.dumps({
                "robot_id": str(robot_id),
                "robot_code": "FR3-SIM-01",
                "work_cell_id": str(work_cell_id),
                "resources_reused": list(RESOURCE_CODES),
                "materials_reused": list(MATERIAL_CODES),
                "capability_checksum": checksum,
                "payload_profile_written": False,
                "payload_profile_reason": "payload_kg가 미확보라 값을 만들지 않음",
            }, ensure_ascii=False, indent=2))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
