import mic from '../assets/mic.svg';
import './voice.css';

// 호출어 음성 명령 막대(2026-10-07). 상태는 wakeVoice.js(useWakeVoice)가 정하고, 여기서는 보여 주기만 한다.
const TEXT = {
  OFF: () => '음성 명령 꺼짐',
  WAKE_WORD_WAITING: (w) => `호출어를 기다리고 있습니다. “${w}”라고 말씀하세요.`,
  LISTENING: () => '듣는 중… 명령을 말씀하세요.',
  TRANSCRIBING: () => '음성을 텍스트로 변환 중',
  ANALYZING: () => '명령 분석 중',
  CONFIRMING: (w, approval) => ({
    speaking: '작업 계획을 안내하는 중입니다',
    listening: '“네·진행해” 또는 “아니·취소해”라고 말씀하세요 — 아래 버튼으로도 됩니다',
    processing: '승인 응답을 서버에 보내는 중입니다',
  }[approval] || '실행 확인 대기 — 아래 카드에서 실행 승인 또는 취소를 누르세요'),
  EXECUTING: () => '작업 실행 중 — ‘정지’라고 말하면 즉시 멈춥니다',
  ERROR: () => '오류 또는 인식 실패',
  STOPPED: () => '정지됨',
};
const TONE = { OFF: 'muted', WAKE_WORD_WAITING: 'info', LISTENING: 'ok', TRANSCRIBING: 'info', ANALYZING: 'info', CONFIRMING: 'warn', EXECUTING: 'info', ERROR: 'danger', STOPPED: 'danger' };

export default function VoiceControl({ voice, recovery }) {
  const { state, wakeWord, unavailable } = voice;
  const isOn = state !== 'OFF';
  return <div className={`voice ${isOn ? 'on' : 'off'}`} role="group" aria-label="음성 명령">
    <div className="voice-row">
      <button type="button" className={`voice-toggle ${isOn ? 'on' : ''}`} aria-pressed={isOn} disabled={!isOn && (!!unavailable || !voice.ready)}
        title={unavailable || (!voice.ready ? '서버 설정을 불러오는 중입니다' : undefined)} onClick={isOn ? voice.off : voice.on}>
        <span className={`voice-mic ${isOn ? 'pulse' : ''}`} aria-hidden="true"><img src={mic} alt="" width="16" height="16" /></span>
        {isOn ? '음성 명령 끄기' : '음성 명령 켜기'}
      </button>
      <small className="voice-wake">현재 호출어: <b>{wakeWord}</b></small>
    </div>
    <p className={`voice-status ${TONE[state]}`} role="status" aria-live="polite">
      <code className="voice-code">{state}</code>{TEXT[state](wakeWord, voice.approval)}
    </p>
    {/* 안내 음성이 다시 인식되지 않게 재생 중에는 마이크를 보내지 않는다 — 그동안은 음성 정지도 듣지 못한다. */}
    {voice.speaking && <small className="voice-note warn" role="status">안내 음성 재생 중 — 이 동안은 음성 ‘정지’를 듣지 않습니다. 급하면 헤더의 ‘즉시 정지’를 누르세요.</small>}
    {voice.answer && state === 'CONFIRMING' && <small className="voice-heard">들은 응답: “{voice.answer}”</small>}
    {voice.partial && <small className="voice-partial">{voice.partial}</small>}
    {voice.heard && state !== 'OFF' && <small className="voice-heard">인식된 명령: “{voice.heard}”</small>}
    {voice.note && <small className={`voice-note ${voice.note.tone}`} role="status">{voice.note.text}</small>}
    {unavailable && <small className="voice-note danger">{unavailable}</small>}
    {state === 'STOPPED' && <div className="voice-recover" role="group" aria-label="정지 후 복구">
      <small>{recovery.summary}</small>
      <div className="voice-actions">
        {recovery.latched && <button type="button" className="btn-secondary" disabled={recovery.busy} onClick={recovery.release}>정지 해제</button>}
        {recovery.canResume && <button type="button" className="btn-secondary" disabled={recovery.busy} onClick={recovery.resume}>▶ 재개</button>}
        {recovery.canRestore && <button type="button" className="btn-secondary" disabled={recovery.busy} onClick={recovery.restore}>↺ 복구(원래 자리로)</button>}
        <button type="button" className="btn-secondary" disabled={recovery.latched} title={recovery.latched ? '정지 해제 뒤 호출어 대기로 돌아갈 수 있습니다' : undefined} onClick={voice.leaveStopped}>호출어 대기로</button>
      </div>
      {recovery.note && <small className="danger">{recovery.note}</small>}
    </div>}
  </div>;
}
