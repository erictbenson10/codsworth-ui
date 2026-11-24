"""Bounded macOS microphone capture; temporary audio is removed after submission."""
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import wave


# Rule 4: say what to do. Naming the pane is not enough — macOS grants the
# permission to a newly started process, so the window must be reopened.
MIC_HELP = ('Allow Ghostty microphone access in System Settings → Privacy & Security → Microphone, '
            'then quit and reopen this window (macOS applies the change only to a newly started '
            'process), and hold Space again.')


def validate_wav(data):
    if len(data) > 20 * 1024 * 1024:
        raise ValueError('Recording exceeded the 20 MB limit')
    with wave.open(io.BytesIO(data), 'rb') as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000:
            raise ValueError('Expected mono 16 kHz PCM16 microphone recording')
        duration = audio.getnframes() / audio.getframerate()
        if duration < .15:
            raise ValueError('Recording was too short. Hold Space while speaking, then release.')
        if duration > 60.1:
            raise ValueError('Recording exceeded the 60 second limit')
        return duration


class Recording:
    def __init__(self, device='0', executable=None):
        executable = executable or shutil.which('ffmpeg')
        if not executable:
            raise RuntimeError('Microphone recording needs FFmpeg. Install it with brew install ffmpeg.')
        if not str(device).isdigit():
            raise ValueError('Microphone device must be its numeric AVFoundation audio index')
        self.directory = tempfile.TemporaryDirectory(prefix='cods-microphone-')
        self.path = Path(self.directory.name) / 'speech.wav'
        self.errors = open(Path(self.directory.name) / 'capture.log', 'w+b')
        self.cancelled = threading.Event()
        self.started = time.monotonic()
        try:
            self.process = subprocess.Popen([executable, '-hide_banner', '-loglevel', 'error', '-nostats',
                '-f', 'avfoundation', '-i', ':' + str(device), '-t', '60', '-ac', '1', '-ar', '16000',
                '-c:a', 'pcm_s16le', '-f', 'wav', '-y', str(self.path)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.errors)
        except Exception:
            self.errors.close()
            self.directory.cleanup()
            raise

    def cancel(self):
        self.cancelled.set()
        try:
            if self.process.poll() is None:
                self.process.kill()
        except OSError:
            pass

    def finish(self):
        try:
            if self.process.poll() is None:
                try:
                    self.process.stdin.write(b'q\n')
                    self.process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                raise RuntimeError('Microphone did not become ready. ' + MIC_HELP) from None
            if self.cancelled.is_set():
                return None
            if self.process.returncode or not self.path.exists():
                raise RuntimeError('Could not record the microphone. ' + MIC_HELP)
            data = self.path.read_bytes()
            try:
                validate_wav(data)
            except (wave.Error, EOFError):
                raise RuntimeError('No usable microphone audio was captured. ' + MIC_HELP) from None
            return data
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            if self.process.stdin:
                self.process.stdin.close()
            self.errors.close()
            self.directory.cleanup()
