/** Ordered one-second input units. Slow links drop whole units, never half a pair. */
import { resample, pcm16 } from "./audio.js";
export class MediaCapture {
  constructor(video, { onChunk, onLevel, onEnded, onBackpressure }) {
    Object.assign(this, { video, onChunk, onLevel, onEnded, onBackpressure });
    this.generation = 0;
    this.stream = null;
    this.context = null;
    this.node = null;
    this.analyser = null;
    this.waveform = null;
    this.pending = new Float32Array(0);
    this.chain = Promise.resolve();
    this.queued = 0;
    this.muted = false;
    this.active = false;
    this.canvas = document.createElement("canvas");
  }
  async start(mode, devices) {
    if (!navigator.mediaDevices?.getUserMedia)
      throw Object.assign(Error("Secure context required"), {
        name: "InsecureContextError",
      });
    const generation = ++this.generation;
    this.mode = mode;
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        ...(devices.microphone
          ? { deviceId: { exact: devices.microphone } }
          : {}),
      },
      video:
        mode === "camera"
          ? {
              width: { ideal: 960 },
              height: { ideal: 540 },
              ...(devices.camera
                ? { deviceId: { exact: devices.camera } }
                : {}),
            }
          : false,
    });
    if (generation !== this.generation) {
      stream.getTracks().forEach((track) => track.stop());
      return false;
    }
    this.stream = stream;
    this.muted = false;
    for (const track of stream.getTracks())
      track.onended = () => {
        if (this.stream === stream) this.onEnded();
      };
    if (mode === "camera") {
      this.video.srcObject = stream;
      await this.video.play();
    }
    const Context = window.AudioContext || window.webkitAudioContext;
    const context = new Context();
    this.context = context;
    await context.resume();
    await context.audioWorklet.addModule("/static/capture-worklet.js");
    if (generation !== this.generation) {
      await context.close().catch(() => {});
      return false;
    }
    const source = context.createMediaStreamSource(stream),
      analyser = context.createAnalyser(),
      node = new AudioWorkletNode(context, "venus-capture"),
      gain = context.createGain();
    analyser.fftSize = 2048;
    this.analyser = analyser;
    this.waveform = new Float32Array(analyser.fftSize);
    gain.gain.value = 0;
    source.connect(analyser).connect(node).connect(gain).connect(context.destination);
    this.node = node;
    node.port.onmessage = ({ data }) => {
      if (generation !== this.generation || !(data instanceof Float32Array))
        return;
      let energy = 0;
      for (const value of data) energy += value * value;
      this.onLevel(this.muted ? 0 : Math.min(1, Math.sqrt(energy / data.length) * 7));
      if (!this.active) {
        this.pending = new Float32Array(0);
        return;
      }
      const pending = new Float32Array(this.pending.length + data.length);
      pending.set(this.pending);
      pending.set(data, this.pending.length);
      this.pending = pending;
      const size = Math.round(context.sampleRate);
      while (this.pending.length >= size) {
        const frame = this.pending.slice(0, size);
        this.pending = this.pending.slice(size);
        this.queue(pcm16(resample(frame, context.sampleRate)), generation);
      }
    };
    return true;
  }
  queue(pcm, generation) {
    if (this.queued >= 3) {
      this.onBackpressure();
      return;
    }
    this.queued++;
    this.chain = this.chain
      .then(async () => {
        if (generation !== this.generation || !this.active) return;
        let jpeg = null;
        if (this.mode === "camera") {
          if (this.video.readyState < 2) return;
          const scale = Math.min(1, 960 / this.video.videoWidth);
          this.canvas.width = Math.max(
            2,
            Math.round(this.video.videoWidth * scale),
          );
          this.canvas.height = Math.max(
            2,
            Math.round(this.video.videoHeight * scale),
          );
          this.canvas
            .getContext("2d")
            .drawImage(this.video, 0, 0, this.canvas.width, this.canvas.height);
          for (const quality of [0.72, 0.58, 0.44, 0.32]) {
            const blob = await new Promise((resolve) =>
              this.canvas.toBlob(resolve, "image/jpeg", quality),
            );
            if (blob && blob.size <= 190 * 1024) {
              jpeg = new Uint8Array(await blob.arrayBuffer());
              break;
            }
          }
          if (!jpeg) {
            this.onBackpressure();
            return;
          }
        }
        if (generation === this.generation && this.active)
          this.onChunk(pcm, jpeg);
      })
      .catch((error) => {
        if (generation === this.generation) this.onEnded(error);
      })
      .finally(() => {
        this.queued--;
      });
  }
  setMuted(value) {
    this.muted = value;
    this.stream?.getAudioTracks().forEach((track) => (track.enabled = !value));
    if (value) this.onLevel(0);
  }
  readWaveform() {
    if (!this.analyser || !this.context || this.muted) return null;
    this.analyser.getFloatTimeDomainData(this.waveform);
    return { samples: this.waveform, sampleRate: this.context.sampleRate };
  }
  async stop() {
    this.generation++;
    this.active = false;
    this.node?.disconnect();
    this.node = null;
    this.analyser?.disconnect();
    this.analyser = null;
    this.waveform = null;
    this.stream?.getTracks().forEach((track) => {
      track.onended = null;
      track.stop();
    });
    this.stream = null;
    this.video.srcObject = null;
    const context = this.context;
    this.context = null;
    if (context) await context.close().catch(() => {});
    this.pending = new Float32Array(0);
    this.onLevel(0);
  }
}
