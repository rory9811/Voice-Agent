/** A quiet, audio-reactive field of light. This is visual decoration, not model state. */
export class Presence {
  constructor(canvas) {
    this.canvas = canvas;
    this.context = canvas.getContext("2d");
    this.inputLevel = 0;
    this.outputLevel = 0;
    this.energy = 0;
    this.phase = "idle";
    this.visible = true;
    this.points = Array.from({ length: 1900 }, (_, i) => {
      const y = 1 - (i / 1899) * 2,
        r = Math.sqrt(1 - y * y),
        angle = i * Math.PI * (3 - Math.sqrt(5));
      return [Math.cos(angle) * r, y, Math.sin(angle) * r, i];
    });
    this.reduced = matchMedia("(prefers-reduced-motion: reduce)");
    this.updateColors();
    window.addEventListener("venus-theme", () => this.updateColors());
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(canvas);
    document.addEventListener("visibilitychange", () => {
      this.visible = !document.hidden;
    });
    this.resize();
    requestAnimationFrame((time) => this.draw(time));
  }
  resize() {
    const rect = this.canvas.getBoundingClientRect(),
      ratio = Math.min(devicePixelRatio || 1, 2);
    this.width = rect.width;
    this.height = rect.height;
    this.canvas.width = Math.round(rect.width * ratio);
    this.canvas.height = Math.round(rect.height * ratio);
    this.context.setTransform(ratio, 0, 0, ratio, 0, 0);
  }
  updateColors() {
    const style = getComputedStyle(document.documentElement);
    this.colors = Object.fromEntries(
      ["cyan", "blue", "ring", "glow"].map((name) => [
        name, style.getPropertyValue(`--orb-${name}`).trim(),
      ]),
    );
    this.accent = style.getPropertyValue("--cyan").trim();
  }
  draw(time) {
    requestAnimationFrame((next) => this.draw(next));
    if (!this.visible) return;
    const ctx = this.context,
      w = this.width,
      h = this.height;
    if (!w || !h) return;
    const seconds = this.reduced.matches ? 0 : time * 0.00022,
      active = this.phase !== "idle" && this.phase !== "ending";
    const target = Math.min(1, Math.max(this.inputLevel, this.outputLevel));
    this.energy += (target - this.energy) * 0.1;
    ctx.clearRect(0, 0, w, h);
    const cx = w / 2,
      cy = h * 0.47,
      r = Math.min(h * 0.35, w * 0.23) * (1 + this.energy * 0.08);
    const glow = ctx.createRadialGradient(cx, cy, r * 0.18, cx, cy, r * 1.6);
    glow.addColorStop(0, `rgba(${this.colors.glow},.08)`);
    glow.addColorStop(0.5, `rgba(${this.colors.glow},.05)`);
    glow.addColorStop(1, `rgba(${this.colors.glow},0)`);
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, w, h);
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(-0.42);
    ctx.strokeStyle = `rgba(${this.colors.ring},.12)`;
    ctx.lineWidth = 0.65;
    ctx.beginPath();
    ctx.ellipse(0, 0, r * 1.39, r * 0.43, 0, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
    const a = seconds * 0.9,
      ca = Math.cos(a),
      sa = Math.sin(a),
      tilt = 0.18;
    const projected = this.points
      .map(([px, py, pz, i]) => {
        const x = px * ca + pz * sa,
          z = -px * sa + pz * ca,
          y = py * Math.cos(tilt) - z * Math.sin(tilt);
        const ripple =
          1 +
          0.035 * Math.sin(py * 7 + seconds * 5) +
          this.energy * 0.08 * Math.sin(px * 10 + seconds * 9);
        const perspective = 1 + z * 0.14;
        return [
          cx + x * r * ripple * perspective,
          cy + y * r * ripple * perspective,
          z,
          i,
        ];
      })
      .sort((a, b) => a[2] - b[2]);
    for (const [x, y, z, i] of projected) {
      const depth = (z + 1) * 0.5;
      const color = i % 9 < 4 ? this.colors.cyan : this.colors.blue;
      const alpha = (0.045 + Math.pow(depth, 2) * 0.59) * (active ? 1.13 : 1);
      ctx.fillStyle = `rgba(${color},${alpha})`;
      ctx.beginPath();
      ctx.arc(x, y, 0.5 + depth * 0.78 + this.energy * 0.22, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(-0.42);
    ctx.strokeStyle = `rgba(${this.colors.ring},.28)`;
    ctx.lineWidth = 0.7;
    ctx.beginPath();
    ctx.ellipse(0, 0, r * 1.39, r * 0.43, 0, 0.1, Math.PI - 0.1);
    ctx.stroke();
    const orbit = seconds * 1.2;
    const ox = Math.cos(orbit) * r * 1.39,
      oy = Math.sin(orbit) * r * 0.43;
    ctx.fillStyle = this.accent;
    ctx.shadowColor = this.accent;
    ctx.shadowBlur = 10;
    ctx.beginPath();
    ctx.arc(ox, oy, 1.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }
}
