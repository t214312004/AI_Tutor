class TutorCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.enabled = false;
    this.port.onmessage = (event) => {
      this.enabled = event.data === true;
    };
  }
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (this.enabled && channel) {
      const copy = channel.slice();
      this.port.postMessage(copy, [copy.buffer]);
    }
    return true;
  }
}
registerProcessor('tutor-capture', TutorCapture);
