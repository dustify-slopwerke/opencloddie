import io
import struct
import unittest

from tests.harness import ROOT  # noqa: F401

from opencloddie.websocket import (
    OP_BINARY,
    OP_CONT,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    WebSocket,
    WSClosed,
    WebSocketError,
    _FrameReader,
    accept_key,
    encode_frame,
)


def reader_for(data: bytes, require_mask: bool = True) -> _FrameReader:
    return _FrameReader(io.BytesIO(data), require_mask=require_mask)


class WebSocketFrameTest(unittest.TestCase):
    def test_rfc_accept_key(self):
        self.assertEqual(
            accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

    def test_masked_roundtrip(self):
        payload = b"hello \x00 world"
        frame = encode_frame(OP_TEXT, payload, mask=True)
        fin, opcode, decoded = reader_for(frame).read_frame()
        self.assertTrue(fin)
        self.assertEqual(opcode, OP_TEXT)
        self.assertEqual(decoded, payload)

    def test_unmasked_roundtrip(self):
        payload = b"x" * 70000
        frame = encode_frame(OP_BINARY, payload, mask=False)
        fin, opcode, decoded = reader_for(frame, require_mask=False).read_frame()
        self.assertTrue(fin)
        self.assertEqual(opcode, OP_BINARY)
        self.assertEqual(decoded, payload)

    def test_server_rejects_unmasked_client_frame(self):
        frame = encode_frame(OP_TEXT, b"hi", mask=False)
        with self.assertRaises(WebSocketError):
            reader_for(frame, require_mask=True).read_frame()

    def test_fragmentation_and_ping(self):
        data = (
            encode_frame(OP_TEXT, b"frag", mask=True, fin=False)
            + encode_frame(OP_PING, b"p", mask=True)
            + encode_frame(OP_CONT, b"mented", mask=True, fin=True)
        )
        rfile = io.BytesIO(data)
        wfile = io.BytesIO()
        ws = WebSocket(rfile, wfile, sock=None)
        opcode, payload = ws.recv()
        self.assertEqual(opcode, OP_TEXT)
        self.assertEqual(payload, b"fragmented")
        # A pong must have been written for the interleaved ping.
        out = wfile.getvalue()
        self.assertTrue(out)
        fin, opcode, pong = reader_for(out, require_mask=False).read_frame()
        self.assertEqual(opcode, OP_PONG)
        self.assertEqual(pong, b"p")

    def test_close_frame(self):
        frame = encode_frame(0x8, struct.pack("!H", 1001) + b"bye", mask=True)
        ws = WebSocket(io.BytesIO(frame), io.BytesIO(), sock=None)
        with self.assertRaises(WSClosed) as ctx:
            ws.recv()
        self.assertEqual(ctx.exception.code, 1001)

    def test_oversize_frame_rejected(self):
        frame = encode_frame(OP_TEXT, b"x" * 20, mask=True)
        reader = _FrameReader(io.BytesIO(frame), require_mask=True, max_message=10)
        with self.assertRaises(WebSocketError):
            reader.read_frame()


if __name__ == "__main__":
    unittest.main()
