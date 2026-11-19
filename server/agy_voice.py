"""The local PCM media path for one agy Live connection generation.

agy 1.5.1 and 1.5.2 open two sockets at ANTIGRAVITY_MIC. The microphone is a raw
16 kHz PCM stream and the speaker sends JETSKI-AUDIO/1 frames. A clear frame
is only a playback queue signal. It does not acknowledge cancellation.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import struct
from typing import Literal


MIC_FRAME_BYTES = 640  # 20 ms, 16 kHz, mono PCM16
MIC_FRAME_SECONDS = .020
SPEAKER_RATE = 24000
MAX_SPEAKER_FRAME_BYTES = 1024 * 1024
_HELLO = b"JETSKI-AUDIO/1 speaker rate=24000"
_ACK = b"JETSKI-AUDIO/1 OK\n"
_SILENCE = bytes(MIC_FRAME_BYTES)
_HEADER = struct.Struct("<BI")
_MIC_IDENTIFY_SECONDS = .3
_IO_TIMEOUT = 2


@dataclass(frozen=True, slots=True)
class AgyAudioEvent:
    kind: Literal["ready", "audio", "clear", "disconnected", "closed"]
    generation: int
    pcm: bytes = b""
    reason: str = ""


class AgyAudioBridge:
    """A bounded, single-use loopback listener. Use a new bridge on reconnect."""

    def __init__(self, generation: int, microphone_frames: int, speaker_events: int):
        self.generation = generation
        self.address = ""
        self._microphone_frames = microphone_frames
        self._speaker_events = speaker_events
        self._input: deque[bytes] = deque()
        self._output: deque[AgyAudioEvent] = deque()
        self._available = asyncio.Event()
        self._media_ready = asyncio.Event()
        self._terminal: AgyAudioEvent | None = None
        self._microphone: asyncio.StreamWriter | None = None
        self._speaker: asyncio.StreamWriter | None = None
        self._speaker_acknowledged = False
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    @classmethod
    async def open(cls, generation: int, *, microphone_frames: int = 20,
                   speaker_events: int = 16) -> AgyAudioBridge:
        if type(generation) is not int or generation < 0:
            raise ValueError("無效的 agy 語音連線世代")
        if (type(microphone_frames) is not int or not 1 <= microphone_frames <= 50
                or type(speaker_events) is not int or not 1 <= speaker_events <= 64):
            raise ValueError("無效的 agy 音訊佇列容量")
        bridge = cls(generation, microphone_frames, speaker_events)
        bridge._server = await asyncio.start_server(
            bridge._accept, host="127.0.0.1", port=0, backlog=2, limit=4096,
        )
        _, port = bridge._server.sockets[0].getsockname()
        bridge.address = f"127.0.0.1:{port}"
        return bridge

    def feed_microphone(self, pcm: bytes, *, generation: int) -> bool:
        """Accept one or more complete frames for the current ready generation."""
        if generation != self.generation or self._terminal or not self._media_ready.is_set():
            return False
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % MIC_FRAME_BYTES:
            raise ValueError("agy 收音須為完整的 20 毫秒 PCM16 片段")
        if len(self._input) + len(pcm) // MIC_FRAME_BYTES > self._microphone_frames:
            self._stop("disconnected", "microphone_overflow")
            return False
        self._input.extend(pcm[i:i + MIC_FRAME_BYTES] for i in range(0, len(pcm), MIC_FRAME_BYTES))
        return True

    def clear_microphone(self, *, generation: int) -> None:
        """Discard unsent input, for example when the student mutes the microphone."""
        if generation == self.generation:
            self._input.clear()

    async def receive(self) -> AgyAudioEvent:
        """Return the next event. Once ended, always return the terminal event."""
        while self._terminal is None and not self._output:
            self._available.clear()
            await self._available.wait()
        if self._terminal:
            return self._terminal
        return self._output.popleft()

    def _emit(self, event: AgyAudioEvent) -> None:
        if self._terminal:
            return
        if len(self._output) >= self._speaker_events:
            self._stop("disconnected", "speaker_overflow")
            return
        self._output.append(event)
        self._available.set()

    def _mark_ready(self) -> None:
        if self._microphone and self._speaker_acknowledged and not self._media_ready.is_set():
            self._emit(AgyAudioEvent("ready", self.generation))
            self._media_ready.set()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._terminal or len(self._writers) >= 2:
            writer.close()
            return
        self._writers.add(writer)
        task = asyncio.create_task(self._serve(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        registered = False
        reason = "media_disconnected"
        try:
            try:
                first = await asyncio.wait_for(reader.read(1), _MIC_IDENTIFY_SECONDS)
            except TimeoutError:
                # The legacy mic peer connects and waits for the bridge to speak first.
                if self._terminal or self._microphone:
                    return
                self._microphone = writer
                registered = True
                self._mark_ready()
                await self._write_microphone(writer)
            else:
                if first != _HELLO[:1] or self._speaker or self._terminal:
                    return
                rest = await asyncio.wait_for(reader.readuntil(b"\n"), _IO_TIMEOUT)
                if first + rest.rstrip(b"\r\n") != _HELLO or self._speaker or self._terminal:
                    return
                self._speaker = writer
                registered = True
                writer.write(_ACK)
                await asyncio.wait_for(writer.drain(), _IO_TIMEOUT)
                self._speaker_acknowledged = True
                self._mark_ready()
                await self._media_ready.wait()
                await self._read_speaker(reader)
        except TimeoutError:
            reason = "media_timeout"
        except (ValueError, asyncio.LimitOverrunError):
            reason = "invalid_speaker_frame"
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            if registered:
                self._stop("disconnected", reason)
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), _IO_TIMEOUT)
            except TimeoutError:
                writer.transport.abort()
            except OSError:
                pass
            finally:
                self._writers.discard(writer)

    async def _write_microphone(self, writer: asyncio.StreamWriter) -> None:
        writer.transport.set_write_buffer_limits(high=8 * MIC_FRAME_BYTES, low=2 * MIC_FRAME_BYTES)
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        while self._terminal is None:
            writer.write(self._input.popleft() if self._input else _SILENCE)
            await asyncio.wait_for(writer.drain(), _IO_TIMEOUT)
            now = loop.time()
            # Keep a small catch-up budget so brief event-loop jitter or clock drift drains backlog instead of overflowing.
            if len(self._input) >= 2:
                deadline = min(deadline + MIC_FRAME_SECONDS * 0.8, now + MIC_FRAME_SECONDS * 0.5)
            else:
                deadline = max(deadline + MIC_FRAME_SECONDS, now - MIC_FRAME_SECONDS * 2)
            delay = deadline - now
            if delay > 0:
                await asyncio.sleep(delay)

    async def _read_speaker(self, reader: asyncio.StreamReader) -> None:
        while self._terminal is None:
            # An idle call is allowed. Time out only after a frame has begun.
            kind = await reader.readexactly(1)
            tail = await asyncio.wait_for(reader.readexactly(_HEADER.size - 1), _IO_TIMEOUT)
            frame_type, size = _HEADER.unpack(kind + tail)
            if frame_type == ord("C") and size == 0:
                self._emit(AgyAudioEvent("clear", self.generation))
            elif frame_type == ord("A") and 0 < size <= MAX_SPEAKER_FRAME_BYTES and size % 2 == 0:
                pcm = await asyncio.wait_for(reader.readexactly(size), _IO_TIMEOUT)
                self._emit(AgyAudioEvent("audio", self.generation, pcm))
            else:
                raise ValueError("無效的 agy 播音幀")

    def _stop(self, kind: Literal["disconnected", "closed"], reason: str = "") -> None:
        if self._terminal:
            return
        self._terminal = AgyAudioEvent(kind, self.generation, reason=reason)
        self._input.clear()
        self._output.clear()
        self._available.set()
        self._media_ready.set()
        if self._server:
            self._server.close()
        for writer in self._writers:
            if kind == "disconnected":
                writer.transport.abort()
            else:
                writer.close()

    async def close(self) -> None:
        self._stop("closed")
        tasks = tuple(self._tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=_IO_TIMEOUT)
            for writer in self._writers:
                writer.transport.abort()
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._server:
            await self._server.wait_closed()
