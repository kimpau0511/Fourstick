"""제3자 자산 manifest (md/개발플랜.md 8-01).

프로젝트가 **자산을 복사하지 않고** 참조만 한다. 여기에는 자산의 출처·버전·
commit·checksum·라이선스와 **로컬 경로**만 들어간다.

규칙:

1. 라이선스가 확인되지 않은 자산은 forstick2에 복사·배포하지 않는다.
   manifest에서 `redistribution=forbidden`, `license_status=unverified`로 표시하고
   외부 제공 자산으로만 취급한다.
2. 자산이 없어도 애플리케이션은 **실패 이유를 분명히 반환한다.**
   `resolve()`가 `asset.missing` / `asset.license_unverified` /
   `asset.checksum_mismatch`를 돌려준다. 없는 자산을 있는 것처럼 대체하지 않는다.
3. checksum이 기록돼 있으면 로컬 파일과 대조한다. 다르면 쓰지 않는다.
4. manifest 자체에 버전이 있다. Profile 기록이 이 버전을 참조한다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from core.reason_codes import ReasonCode


class AssetManifestError(Exception):
    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


class LicenseStatus(str, Enum):
    """라이선스 확인 상태."""

    #: SPDX 식별자까지 확인됐다.
    CONFIRMED = "confirmed"
    #: 선언은 있으나 원문·범위를 확인하지 않았다.
    DECLARED = "declared"
    #: 확인되지 않았다. 재배포 금지로 취급한다.
    UNVERIFIED = "unverified"

    def __str__(self) -> str:
        return self.value


class Redistribution(str, Enum):
    ALLOWED = "allowed"
    FORBIDDEN = "forbidden"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


class AssetKind(str, Enum):
    GIT_REPO = "git_repo"
    GIT_FILE = "git_file"
    ROS_PACKAGE = "ros_package"
    DOCUMENT = "document"
    ENVIRONMENT = "environment"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class AssetEntry:
    """자산 하나의 출처·버전·라이선스. **내용은 담지 않는다.**"""

    asset_id: str
    kind: AssetKind
    #: 사람이 찾아갈 수 있는 출처(저장소 URL, 패키지 이름, 문서 제목).
    source: str
    #: 저장소 안의 경로(파일 자산일 때).
    path: str = ""
    #: 태그·패키지 버전.
    version: str = ""
    #: commit SHA(저장소 자산) — **정확한 commit을 기록한다.**
    commit: str = ""
    #: checksum. `"<종류>:<값>"` 형태(`sha256:…`, `git-blob:…`).
    checksum: str = ""
    license_spdx: str = ""
    license_status: LicenseStatus = LicenseStatus.UNVERIFIED
    redistribution: Redistribution = Redistribution.UNKNOWN
    #: 로컬에 있으면 그 경로. 없으면 빈 값(= 외부 제공 대기).
    local_path: str = ""
    #: 이 자산으로 무엇을 하는가(감사용).
    usage: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if not self.asset_id or not self.source:
            raise AssetManifestError(
                ReasonCode.CONFIG_MISSING, "asset_id와 source는 필수다"
            )
        if not isinstance(self.kind, AssetKind):
            raise AssetManifestError(
                ReasonCode.CONFIG_INVALID, f"kind가 낯설다: {self.kind!r}"
            )
        if self.license_status is LicenseStatus.CONFIRMED and not self.license_spdx:
            raise AssetManifestError(
                ReasonCode.CONFIG_INVALID,
                f"{self.asset_id}: 라이선스 confirmed인데 SPDX 식별자가 없다",
            )
        if (
            self.license_status is LicenseStatus.UNVERIFIED
            and self.redistribution is Redistribution.ALLOWED
        ):
            raise AssetManifestError(
                ReasonCode.CONFIG_INVALID,
                f"{self.asset_id}: 라이선스가 확인되지 않았는데 재배포 허용으로"
                " 적혀 있다 — 확인되지 않은 자산은 금지로 취급한다",
            )
        if self.checksum and ":" not in self.checksum:
            raise AssetManifestError(
                ReasonCode.CONFIG_INVALID,
                f"{self.asset_id}: checksum은 '<종류>:<값>' 형태여야 한다",
            )

    @property
    def may_copy_into_repo(self) -> bool:
        """저장소에 복사해도 되는가. 확인되지 않으면 False다."""
        return (
            self.redistribution is Redistribution.ALLOWED
            and self.license_status in (LicenseStatus.CONFIRMED, LicenseStatus.DECLARED)
        )

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind.value,
            "source": self.source,
            "path": self.path,
            "version": self.version,
            "commit": self.commit,
            "checksum": self.checksum,
            "license_spdx": self.license_spdx,
            "license_status": self.license_status.value,
            "redistribution": self.redistribution.value,
            "local_path": self.local_path,
            "may_copy_into_repo": self.may_copy_into_repo,
            "usage": self.usage,
            "note": self.note,
        }


@dataclass(frozen=True)
class AssetStatus:
    """자산 조회 결과. 없으면 **이유를 담아** 돌려준다."""

    asset_id: str
    available: bool
    reason: ReasonCode | None = None
    detail: str = ""
    resolved_path: str = ""

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "available": self.available,
            "reason_code": None if self.reason is None else self.reason.value,
            "detail": self.detail,
            "resolved_path": self.resolved_path,
        }


@dataclass(frozen=True)
class AssetManifest:
    manifest_version: str
    entries: tuple[AssetEntry, ...]
    note: str = ""

    def __post_init__(self) -> None:
        if not self.manifest_version:
            raise AssetManifestError(
                ReasonCode.CONFIG_MISSING, "manifest_version이 없다"
            )
        ids = [e.asset_id for e in self.entries]
        if len(set(ids)) != len(ids):
            raise AssetManifestError(
                ReasonCode.CONFIG_INVALID, f"중복 asset_id: {ids}"
            )

    def get(self, asset_id: str) -> AssetEntry | None:
        for entry in self.entries:
            if entry.asset_id == asset_id:
                return entry
        return None

    def resolve(self, asset_id: str) -> AssetStatus:
        """자산을 실제로 쓸 수 있는지 확인한다.

        확인 순서: manifest 등재 → 라이선스 → 로컬 경로 존재 → checksum.
        어느 단계에서 막히든 **이유 코드를 붙여** 돌려준다.
        """
        entry = self.get(asset_id)
        if entry is None:
            return AssetStatus(
                asset_id, False, ReasonCode.ASSET_MISSING,
                f"manifest({self.manifest_version})에 없는 자산이다",
            )
        if entry.license_status is LicenseStatus.UNVERIFIED:
            return AssetStatus(
                asset_id, False, ReasonCode.ASSET_LICENSE_UNVERIFIED,
                f"{entry.source} 라이선스가 확인되지 않았다 — 복사·배포하지 않고"
                " 외부 제공 자산으로만 쓴다",
            )
        if entry.kind is AssetKind.ENVIRONMENT:
            # 환경 기록(설치된 배포판·버전)은 파일이 아니다.
            return AssetStatus(asset_id, True, None, "환경 기록", entry.local_path)
        if not entry.local_path:
            return AssetStatus(
                asset_id, False, ReasonCode.ASSET_MISSING,
                "로컬 경로가 없다 — 사용자가 제공해야 한다",
            )
        path = Path(entry.local_path)
        if not path.exists():
            return AssetStatus(
                asset_id, False, ReasonCode.ASSET_MISSING,
                f"경로가 없다: {entry.local_path}",
            )
        if entry.checksum.startswith("sha256:") and path.is_file():
            actual = sha256_of(path)
            expected = entry.checksum.split(":", 1)[1]
            if actual != expected:
                return AssetStatus(
                    asset_id, False, ReasonCode.ASSET_CHECKSUM_MISMATCH,
                    f"checksum이 다르다 (기대 {expected[:12]}…, 실제 {actual[:12]}…)",
                )
        return AssetStatus(asset_id, True, None, "", str(path))

    def statuses(self) -> tuple[AssetStatus, ...]:
        return tuple(self.resolve(e.asset_id) for e in self.entries)

    def unverified_licenses(self) -> tuple[str, ...]:
        return tuple(
            e.asset_id for e in self.entries
            if e.license_status is LicenseStatus.UNVERIFIED
        )

    def copyable(self) -> tuple[str, ...]:
        return tuple(e.asset_id for e in self.entries if e.may_copy_into_repo)

    def to_dict(self) -> dict:
        return {
            "manifest_version": self.manifest_version,
            "note": self.note,
            "assets": [e.to_dict() for e in self.entries],
            "unverified_licenses": list(self.unverified_licenses()),
        }


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
