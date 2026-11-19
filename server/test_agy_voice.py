"""Loopback media tests with synthetic PCM and no agy process."""
import asyncio
from contextlib import suppress
import struct
import unittest

from .agy_voice import AgyAudioBridge, AgyAudioEvent, MAX_SPEAKER_FRAME_BYTES, MIC_FRAME_BYTES


def wire_frame(kind: str, pcm: bytes = b"") -> bytes:
    return struct.pack("<cI", kind.encode("ascii"), len(pcm)) + pcm


class AgyAudioBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def bridge(self, **kwargs):
        bridge = await AgyAudioBridge.open(7, **kwargs)
        self.addAsyncCleanup(bridge.close)
        return bridge

    async def connect(self, bridge):
        host, port = bridge.address.split(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        self.addAsyncCleanup(self.close_writer, writer)
        return reader, writer

    @staticmethod
    async def close_writer(writer):
        writer.close()
        with suppress(OSError):
            await asyncio.wait_for(writer.wait_closed(), 3)

    async def media_peers(self, bridge, *, receive_ready=True):
        speaker, speaker_writer = await self.connect(bridge)
        speaker_writer.write(b"JET")
        await speaker_writer.drain()
        await asyncio.sleep(.01)
        speaker_writer.write(b"SKI-AUDIO/1 speaker rate=24000\n")
        self.assertEqual(await asyncio.wait_for(speaker.readline(), 3), b"JETSKI-AUDIO/1 OK\n")
        microphone, _ = await self.connect(bridge)
        if receive_ready:
            self.assertEqual(await self.event(bridge), AgyAudioEvent("ready", 7))
        return speaker, speaker_writer, microphone

    async def event(self, bridge):
        return await asyncio.wait_for(bridge.receive(), 3)

    async def test_speaker_receives_fragmented_audio_and_clear_in_wire_order(self):
        bridge = await self.bridge()
        _, speaker, _ = await self.media_peers(bridge)
        pcm = struct.pack("<hhh", 32, -38, 64)
        data = wire_frame("A", pcm)
        speaker.write(data[:3])
        await speaker.drain()
        await asyncio.sleep(.01)
        speaker.write(data[3:] + wire_frame("C"))
        await speaker.drain()
        self.assertEqual(await self.event(bridge), AgyAudioEvent("audio", 7, pcm))
        self.assertEqual(await self.event(bridge), AgyAudioEvent("clear", 7))

    async def test_microphone_writes_only_current_generation_and_clears_unsent_input(self):
        bridge = await self.bridge()
        retired = struct.pack("<h", 101) * (MIC_FRAME_BYTES // 2)
        self.assertFalse(bridge.feed_microphone(retired, generation=7))
        _, _, microphone = await self.media_peers(bridge)
        self.assertFalse(bridge.feed_microphone(retired, generation=6))
        self.assertTrue(bridge.feed_microphone(retired, generation=7))
        bridge.clear_microphone(generation=7)
        first = struct.pack("<h", 20) * (MIC_FRAME_BYTES // 2)
        second = struct.pack("<h", -25) * (MIC_FRAME_BYTES // 2)
        self.assertTrue(bridge.feed_microphone(first + second, generation=7))

        async def next_nonzero_frame():
            silence = bytes(MIC_FRAME_BYTES)
            while True:
                frame = await microphone.readexactly(MIC_FRAME_BYTES)
                if frame != silence:
                    return frame

        self.assertEqual(await asyncio.wait_for(next_nonzero_frame(), 3), first)
        self.assertEqual(await asyncio.wait_for(next_nonzero_frame(), 3), second)

    async def test_incomplete_speaker_payload_is_never_delivered(self):
        bridge = await self.bridge()
        _, speaker, _ = await self.media_peers(bridge)
        speaker.write(struct.pack("<cI", b"A", 8) + b"\x01\x02")
        await speaker.drain()
        await self.close_writer(speaker)
        self.assertEqual(await self.event(bridge), AgyAudioEvent("disconnected", 7, reason="media_disconnected"))

    async def test_oversized_speaker_header_ends_media_without_reading_payload(self):
        bridge = await self.bridge()
        _, speaker, _ = await self.media_peers(bridge)
        speaker.write(struct.pack("<cI", b"A", MAX_SPEAKER_FRAME_BYTES + 2))
        await speaker.drain()
        self.assertEqual(await self.event(bridge), AgyAudioEvent("disconnected", 7, reason="invalid_speaker_frame"))

    async def test_backpressure_ends_the_generation_without_returning_unheard_pcm(self):
        bridge = await self.bridge(speaker_events=1)
        speaker, speaker_writer, microphone = await self.media_peers(bridge, receive_ready=False)
        await asyncio.wait_for(microphone.readexactly(MIC_FRAME_BYTES), 3)
        # Leave the ready event queued so the next speaker frame has no space.
        speaker_writer.write(wire_frame("A", b"\x00\x01"))
        await speaker_writer.drain()
        self.assertEqual(await asyncio.wait_for(speaker.read(), 3), b"")
        self.assertEqual(await self.event(bridge), AgyAudioEvent("disconnected", 7, reason="speaker_overflow"))

    async def test_microphone_overflow_rejects_the_entire_chunk(self):
        bridge = await self.bridge(microphone_frames=1)
        await self.media_peers(bridge)
        self.assertFalse(bridge.feed_microphone(bytes(2 * MIC_FRAME_BYTES), generation=7))
        self.assertEqual(await self.event(bridge), AgyAudioEvent("disconnected", 7, reason="microphone_overflow"))

    async def test_close_wakes_waiters_and_releases_listener_and_both_peers(self):
        bridge = await self.bridge()
        speaker, _, microphone = await self.media_peers(bridge)
        waiting = asyncio.create_task(self.event(bridge))
        await bridge.close()
        self.assertEqual(await waiting, AgyAudioEvent("closed", 7))
        self.assertEqual(await asyncio.wait_for(speaker.read(), 3), b"")
        await asyncio.wait_for(microphone.read(), 3)
        self.assertTrue(microphone.at_eof())
        self.assertFalse(bridge.feed_microphone(bytes(MIC_FRAME_BYTES), generation=7))
        host, port = bridge.address.split(":")
        with self.assertRaises(OSError):
            await asyncio.wait_for(asyncio.open_connection(host, int(port)), 3)


if __name__ == "__main__":
    unittest.main()
