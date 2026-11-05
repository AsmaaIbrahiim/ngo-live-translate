class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.outRate = 16000;
    this.ratio = sampleRate / this.outRate;
    this.buffer = [];
  }

  process(inputs) {
    const input = inputs[0];
    if (input.length === 0) return true;
    const channel = input[0];

    for (let i = 0; i < channel.length; i++) {
      this.buffer.push(channel[i]);
    }

    const outLength = Math.floor(this.buffer.length / this.ratio);
    if (outLength > 0) {
      const out = new Int16Array(outLength);
      for (let i = 0; i < outLength; i++) {
        const srcIndex = Math.floor(i * this.ratio);
        const s = Math.max(-1, Math.min(1, this.buffer[srcIndex]));
        out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(out.buffer, [out.buffer]);
      this.buffer = this.buffer.slice(Math.floor(outLength * this.ratio));
    }

    return true;
  }
}

registerProcessor("pcm-processor", PCMProcessor);
