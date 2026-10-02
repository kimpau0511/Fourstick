// Playwright JSON 결과(e2e-results/results.json) → 요구사항 ID별 결과 표(마크다운).
//   node e2e/report.mjs [출력 파일]   출력 파일을 주지 않으면 화면에만 찍는다.
// 판정: 통과 / 실패 / 미실행(서버 없음 등으로 건너뜀). 건너뛴 것을 통과로 세지 않는다.
import { readFileSync, writeFileSync } from 'node:fs';

const data = JSON.parse(readFileSync(new URL('../e2e-results/results.json', import.meta.url), 'utf-8'));
const rows = [];
function walk(suite, project) {
  for (const spec of suite.specs || []) {
    for (const t of spec.tests || []) {
      const r = t.results.at(-1) || {};
      const status = t.status === 'skipped' || r.status === 'skipped' ? '미실행'
        : r.status === 'passed' ? '통과' : '실패';
      const err = (r.error?.message || '').replace(/\u001b\[[0-9;]*m/g, '').split('\n').find((l) => l.trim()) || '';
      const notes = (t.annotations || []).concat(r.annotations || []).map((a) => `${a.type}: ${a.description}`).join(' / ');
      const [, id = '', req = ''] = spec.title.match(/^\[([^\]]+)\](?:\[([^\]]+)\])?/) || [];
      rows.push({ project: t.projectName || project, id, req, title: spec.title.replace(/^(\[[^\]]+\])+\s*/, ''), status, err: err.slice(0, 140), notes });
    }
  }
  for (const s of suite.suites || []) walk(s, project);
}
for (const s of data.suites) walk(s, '');

const count = (st) => rows.filter((r) => r.status === st).length;
const lines = [
  `| 통과 | 실패 | 미실행 | 합계 |`, `|---|---|---|---|`,
  `| ${count('통과')} | ${count('실패')} | ${count('미실행')} | ${rows.length} |`, '',
  '| 프로젝트 | 테스트 ID | 요구사항 | 내용 | 결과 | 실패 원인·측정값 |', '|---|---|---|---|---|---|',
  ...rows.sort((a, b) => a.project.localeCompare(b.project) || a.id.localeCompare(b.id))
    .map((r) => `| ${r.project} | ${r.id} | ${r.req} | ${r.title} | ${r.status} | ${(r.status === '실패' ? r.err : r.notes).replace(/\|/g, '\\|')} |`),
];
const out = lines.join('\n');
if (process.argv[2]) writeFileSync(process.argv[2], out + '\n', 'utf-8');
console.log(out);
