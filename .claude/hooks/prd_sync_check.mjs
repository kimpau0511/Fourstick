// 세션 시작 때 저장소 밖 기획서가 docs/PRD.md에 반영된 판과 다른지 확인한다.
// 기획서 폴더가 없는 PC(다른 팀원)에서는 아무것도 하지 않는다 — 원본이 없으면 비교할 게 없다.
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const repo = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const source = join(repo, "..", "문서", "기획서_템플릿(포스틱).md");
const prd = join(repo, "docs", "PRD.md");

if (!existsSync(source) || !existsSync(prd)) process.exit(0);

const current = createHash("sha256").update(readFileSync(source)).digest("hex").slice(0, 12);
const recorded = readFileSync(prd, "utf8").match(/원본 해시[^`]*`([0-9a-f]{12})`/)?.[1];

if (current !== recorded) {
  const msg = `기획서(../문서/기획서_템플릿(포스틱).md)가 docs/PRD.md에 기록된 판(${recorded ?? "없음"})과 다릅니다(현재 ${current}). ` +
    "바뀐 내용을 PRD에 반영하고 PRD의 원본 해시를 갱신하세요.";
  console.log(JSON.stringify({
    systemMessage: msg,
    hookSpecificOutput: { hookEventName: "SessionStart", additionalContext: msg },
  }));
}
