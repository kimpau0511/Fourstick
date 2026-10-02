// AudioWorklet: 브라우저 오디오를 백엔드 계약(PCM16 mono)으로 바꿔 보낸다.
//
// 백엔드가 요구하는 것은 PCM16 little-endian, mono, 설정된 sample rate다.
// AudioContext를 그 sample rate로 만들기 때문에 여기서 리샘플링하지 않는다 —
// 리샘플링을 흉내 내면 실제로 보내는 오디오와 기록이 어긋난다.
//
// Silero VAD가 512 샘플 단위로 판정하므로 그 배수로 모아 보낸다. 128 샘플씩
// 오는 렌더 쿼텀을 바로 보내면 프레임이 잘려 판정을 못 한다.
const FRAME_SAMPLES = 512;

class PcmFrameProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(FRAME_SAMPLES);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    for (let i = 0; i < channel.length; i += 1) {
      this.buffer[this.filled] = channel[i];
      this.filled += 1;
      if (this.filled === FRAME_SAMPLES) {
        const pcm = new Int16Array(FRAME_SAMPLES);
        for (let s = 0; s < FRAME_SAMPLES; s += 1) {
          const clamped = Math.max(-1, Math.min(1, this.buffer[s]));
          pcm[s] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
        }
        this.port.postMessage(pcm.buffer, [pcm.buffer]);
        this.filled = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-frame-processor", PcmFrameProcessor);
