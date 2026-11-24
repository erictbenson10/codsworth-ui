/* A synthesized, nonvoice test signal. No recording is stored or uploaded. */
function tone(seconds = 20) {
  const sampleRate = 16000, frames = sampleRate * seconds;
  const buffer = Buffer.alloc(44 + frames * 2);
  buffer.write('RIFF', 0); buffer.writeUInt32LE(buffer.length - 8, 4);
  buffer.write('WAVEfmt ', 8); buffer.writeUInt32LE(16, 16);
  buffer.writeUInt16LE(1, 20); buffer.writeUInt16LE(1, 22);
  buffer.writeUInt32LE(sampleRate, 24); buffer.writeUInt32LE(sampleRate * 2, 28);
  buffer.writeUInt16LE(2, 32); buffer.writeUInt16LE(16, 34);
  buffer.write('data', 36); buffer.writeUInt32LE(frames * 2, 40);
  for (let i = 0; i < frames; i++) buffer.writeInt16LE(Math.round(Math.sin(i * 2 * Math.PI * 440 / sampleRate) * 300), 44 + i * 2);
  return buffer;
}

async function selectAudio(page, name = 'synthetic-test-tone.wav') {
  await page.locator('#audio-file').setInputFiles({name, mimeType:'audio/wav', buffer:tone()});
  await page.waitForFunction(() => document.querySelector('#voice').readyState >= 1);
}

module.exports = {tone, selectAudio};
