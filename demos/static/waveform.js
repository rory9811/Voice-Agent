/** Draw the actual microphone samples, independently of model speech or network speed. */
export class MicrophoneWaveform {
  constructor(canvas) {
    this.canvas = canvas;
    this.context = canvas.getContext("2d");
    this.active = false;
    this.muted = false;
    new ResizeObserver(() => this.resize()).observe(canvas);
  }
  resize() {
    const rect = this.canvas.getBoundingClientRect();
    const ratio = Math.min(devicePixelRatio || 1, 2);
    this.width = rect.width;
    this.height = rect.height;
    this.canvas.width = Math.round(rect.width * ratio);
    this.canvas.height = Math.round(rect.height * ratio);
    this.context.setTransform(ratio, 0, 0, ratio, 0, 0);
  }
  setState(active, muted) {
    this.active = active;
    this.muted = muted;
  }
  draw(frame) {
    if (!this.active || document.hidden) return;
    const ctx = this.context, w = this.width, h = this.height;
    if (!w || !h) return;
    const samples = this.muted ? null : frame?.samples;
    let peak = 0, energy = 0;
    if (samples) {
      for (const value of samples) {
        peak = Math.max(peak, Math.abs(value));
        energy += value * value;
      }
    }
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = "rgba(255, 255, 255, 0.2)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, h / 2);
    ctx.lineTo(w, h / 2);
    ctx.stroke();
    if (!samples || Math.sqrt(energy / samples.length) < 0.0025) return;

    // A fixed 20 ms window shows pitch as wave spacing and loudness as height.
    // Start at a rising zero crossing to keep sustained notes visually steady.
    const count = Math.min(Math.round(frame.sampleRate * 0.02), samples.length);
    let offset = 0;
    for (let i = 1; i < samples.length - count; i++) {
      if (samples[i - 1] <= 0 && samples[i] > 0) { offset = i; break; }
    }
    const gain = 3.8 / Math.sqrt(Math.max(0.06, peak));
    ctx.strokeStyle = "#4ade80";
    ctx.lineWidth = 1.6;
    ctx.lineJoin = "round";
    ctx.beginPath();
    for (let i = 0; i < count; i++) {
      const x = (i / (count - 1)) * w;
      const y = h / 2 - Math.tanh(samples[offset + i] * gain) * h * 0.42;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
}
