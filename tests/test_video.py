"""M1/M2：用 ffmpeg 生成一段带变化和音频的 MP4，验证 PyAV 解码、抽帧、门控触发与音频分块。"""
import shutil
import subprocess

import pytest

from pa.gate import FrameGate, GateCfg
from pa.source import VideoSource

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")


@pytest.fixture(scope="module")
def mp4(tmp_path_factory):
    p = tmp_path_factory.mktemp("v") / "clip.mp4"
    # 前 3 秒黑屏，之后切到彩条：门控应在 3 s 附近触发 change 窗口
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", "color=c=black:s=640x360:r=25:d=3",
           "-f", "lavfi", "-i", "testsrc=s=640x360:r=25:d=5",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
           "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)]
    subprocess.run(cmd, check=True)
    return str(p)


def test_decode_sample_and_gate(mp4):
    src = VideoSource(mp4, sample_fps=5, short_side=224)
    frames = list(src.frames())
    assert 35 <= len(frames) <= 42
    assert min(frames[0].img.shape[:2]) == 224
    gate = FrameGate(GateCfg(fps=5, heartbeat_s=4))
    reqs = []
    for i, f in enumerate(frames):
        reqs += gate.push(f"f_{i:06d}", f.pts_ms, f.img)
    triggers = [(r.trigger, r.end_ms) for r in reqs]
    assert triggers[0][0] == "initial"
    change = [t for trig, t in triggers if trig == "change"]
    assert change and 3000 <= change[0] <= 4600, triggers


def test_srt_speaker_and_availability(tmp_path):
    from pa.speech import SrtUtterances
    p = tmp_path / "x.srt"
    srt = ("1\n00:00:19,433 --> 00:00:20,233\nMarry:hmm\n\n"
           "2\n00:00:20,633 --> 00:00:23,200\nMarry:the cappuccino today is very delicious\n")
    p.write_text(srt, encoding="utf-8")
    u = SrtUtterances(p, latency_ms=500).all()
    assert [x.voice_anchor for x in u] == ["Marry", "Marry"]
    assert u[1].text.startswith("the cappuccino") and u[1].t_avail_ms == 23700


def test_seek_and_audio_chunks(mp4):
    src = VideoSource(mp4, sample_fps=5, short_side=224, start_s=4.0, duration_s=2.0)
    frames = list(src.frames())
    assert frames and frames[0].pts_ms < 300 and frames[-1].pts_ms <= 2000
    chunks = list(VideoSource(mp4).audio(chunk_s=2.0))
    assert len(chunks) >= 3 and chunks[0].pcm.dtype.name == "float32"


def test_live_http_stream_frames_arrive_in_wall_clock(mp4):
    """ffmpeg 以 HTTP 服务端方式实时推流（-re），LiveSource 连接后按到达时刻取帧。"""
    import socket
    import time

    from pa.source.live import LiveSource
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-re", "-i", mp4, "-t", "4", "-c:v", "libx264",
                            "-preset", "ultrafast", "-tune", "zerolatency", "-an", "-f", "mpegts", "-listen", "1",
                            f"http://127.0.0.1:{port}"])
    try:
        time.sleep(1.0)
        src = LiveSource(f"http://127.0.0.1:{port}", sample_fps=5, short_side=224, open_timeout_s=10)
        t0 = time.perf_counter()
        src.start(t0)
        got = []
        while not src.done and time.perf_counter() - t0 < 20:
            got += src.drain()
            time.sleep(0.05)
        got += src.drain()
        assert src.error is None, src.error
        assert 12 <= len(got) <= 25, len(got)  # 约 4 s × 5 fps
        span = got[-1].pts_ms - got[0].pts_ms
        assert 2500 <= span <= 6000, span  # 帧按墙钟到达，而不是瞬间读完
    finally:
        srv.kill()
