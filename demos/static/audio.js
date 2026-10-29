/** PCM conversion and playback receipts tied to the current conversation. */
export function resample(input, sourceRate, targetRate = 16000) {
  if (sourceRate === targetRate) return input;
  const ratio = sourceRate / targetRate,
    output = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < output.length; i++) {
    const start = i * ratio,
      end = Math.min(input.length, (i + 1) * ratio);
    let total = 0;
    for (let j = Math.floor(start); j < Math.ceil(end); j++)
      total += input[j] * (Math.min(end, j + 1) - Math.max(start, j));
    output[i] = total / (end - start);
  }
  return output;
}
export function pcm16(samples) {
  const bytes = new Uint8Array(samples.length * 2),
    view = new DataView(bytes.buffer);
  samples.forEach((value, i) => {
    const v = Math.max(-1, Math.min(1, value));
    view.setInt16(i * 2, v < 0 ? v * 32768 : v * 32767, true);
  });
  return bytes;
}
export function encode(bytes) {
  let result = "";
  for (let i = 0; i < bytes.length; i += 32768)
    result += String.fromCharCode(...bytes.subarray(i, i + 32768));
  return btoa(result);
}
export function decode(value) {
  const text = atob(value),
    bytes = new Uint8Array(text.length);
  for (let i = 0; i < text.length; i++) bytes[i] = text.charCodeAt(i);
  return bytes;
}
export class AudioPlayer {
  constructor({
    ack,
    onText,
    onActivity,
    onError,
    // A fresh start plays this far in the future, so a later chunk may arrive
    // up to this late without a gap. Simulated with measured step times
    // (p50 365 ms, p95 906 ms): 1.0 s leaves a gap in ~4% of 10 s answers.
    leadSeconds = 1.0,
    maxQueuedSeconds = 60,
  }) {
    if (
      !Number.isFinite(leadSeconds) ||
      leadSeconds < 0 ||
      !Number.isFinite(maxQueuedSeconds) ||
      maxQueuedSeconds <= leadSeconds
    )
      throw Error("Invalid playback buffer settings");
    this.ack = ack;
    this.onText = onText;
    this.onActivity = onActivity;
    this.onError = onError;
    this.context = null;
    this.sources = new Set();
    this.audibleSources = new Set();
    this.cursor = 0;
    this.epoch = 0;
    this.muted = false;
    this.chain = Promise.resolve();
    this.level = 0;
    this.leadSeconds = leadSeconds;
    this.maxQueuedSeconds = maxQueuedSeconds;
  }
  async prime() {
    if (!this.context) {
      const Context = window.AudioContext || window.webkitAudioContext;
      this.context = new Context();
      this.gain = this.context.createGain();
      this.analyser = this.context.createAnalyser();
      this.analyser.fftSize = 256;
      this.gain.connect(this.analyser).connect(this.context.destination);
      this.samples = new Uint8Array(this.analyser.fftSize);
    }
    await this.context.resume();
  }
  setMuted(value) {
    this.muted = value;
    if (this.gain) this.gain.gain.value = value ? 0 : 1;
  }
  clear() {
    this.epoch++;
    for (const source of this.sources) {
      source.onended = null;
      try {
        source.stop();
        source.disconnect();
      } catch {}
    }
    this.sources.clear();
    this.audibleSources.clear();
    this.cursor = 0;
    this.level = 0;
    this.chain = Promise.resolve();
    this.onActivity(false);
  }
  enqueue(message) {
    const epoch = this.epoch;
    this.chain = this.chain
      .then(() => this.play(message, epoch))
      .catch((error) => {
        if (epoch === this.epoch) this.onError(error);
      });
  }
  async play(message, epoch) {
    if (epoch !== this.epoch) return;
    await this.prime();
    if (epoch !== this.epoch) return;
    const bytes = decode(message.data);
    let buffer;
    if (message.format === "wav") {
      buffer = await this.context.decodeAudioData(bytes.buffer.slice(0));
    } else if (message.format === "pcm_s16le") {
      const channels = message.channels,
        frames = bytes.length / (2 * channels);
      if (
        !Number.isInteger(channels) ||
        channels < 1 ||
        channels > 2 ||
        !Number.isInteger(frames) ||
        frames < 1
      )
        throw Error("Invalid audio chunk");
      buffer = this.context.createBuffer(channels, frames, message.sample_rate);
      const view = new DataView(bytes.buffer);
      for (let ch = 0; ch < channels; ch++) {
        const samples = buffer.getChannelData(ch);
        for (let i = 0; i < frames; i++)
          samples[i] = view.getInt16((i * channels + ch) * 2, true) / 32768;
      }
    } else throw Error("Unsupported audio format");
    if (epoch !== this.epoch) return;
    const now = this.context.currentTime;
    if (Math.max(0, this.cursor - now) + buffer.duration > this.maxQueuedSeconds)
      throw Error("Playback buffer is full");
    if (message.text) this.onText(message.text, message.utterance_id);
    // Only a fresh start needs lead time. All subsequent buffers share one
    // sample clock, independent of network arrival times and chunk lengths.
    if (this.cursor <= now) this.cursor = now + this.leadSeconds;
    this.schedule({ message, buffer, epoch });
    this.onActivity(this.audibleSources.size > 0);
  }
  schedule({ message, buffer, epoch }) {
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.gain);
    source.onended = () => {
      this.sources.delete(source);
      this.audibleSources.delete(source);
      source.disconnect();
      if (epoch !== this.epoch) return;
      this.ack({
        type: "playback_ack",
        utterance_id: message.utterance_id,
        chunks: message.chunk_seq,
      });
      if (!this.audibleSources.size) this.onActivity(false);
    };
    this.sources.add(source);
    if (message.silent !== true) this.audibleSources.add(source);
    source.start(this.cursor);
    this.cursor += buffer.duration;
  }
  amplitude() {
    if (!this.analyser || !this.sources.size) return 0;
    this.analyser.getByteTimeDomainData(this.samples);
    let energy = 0;
    for (const value of this.samples) energy += (value - 128) ** 2;
    return Math.min(1, Math.sqrt(energy / this.samples.length) / 34);
  }
}
