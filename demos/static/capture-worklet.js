class VenusCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(2048);
    this.offset = 0;
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    let position = 0;
    while (position < input.length) {
      const count = Math.min(
        input.length - position,
        this.buffer.length - this.offset,
      );
      this.buffer.set(input.subarray(position, position + count), this.offset);
      this.offset += count;
      position += count;
      if (this.offset === this.buffer.length) {
        this.port.postMessage(this.buffer, [this.buffer.buffer]);
        this.buffer = new Float32Array(2048);
        this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor("venus-capture", VenusCapture);
