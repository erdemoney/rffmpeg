import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "rffmpeg"
# These tests exercise pure routing logic; the CLI and YAML dependencies are
# only needed by other entry points in the executable.
loader = importlib.machinery.SourceFileLoader("rffmpeg_module", str(MODULE_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
rffmpeg = importlib.util.module_from_spec(spec)
with patch.dict(
    sys.modules,
    {"click": ModuleType("click"), "yaml": ModuleType("yaml")},
):
    loader.exec_module(rffmpeg)


class ClassifyFfmpegJobTests(unittest.TestCase):
    def test_remux_with_hls_output(self):
        args = [
            "-i",
            "input.mkv",
            "-map",
            "0:0",
            "-map",
            "0:1",
            "-codec:v:0",
            "copy",
            "-codec:a:0",
            "copy",
            "-f",
            "hls",
            "out.m3u8",
        ]
        self.assertEqual(rffmpeg.classify_ffmpeg_job(args), "remux")

    def test_audio_transcode_with_video_copy(self):
        args = [
            "-i",
            "input.mkv",
            "-codec:v:0",
            "copy",
            "-codec:a:0",
            "aac",
            "-f",
            "hls",
            "out.m3u8",
        ]
        self.assertEqual(rffmpeg.classify_ffmpeg_job(args), "audio_transcode")

    def test_video_transcode_with_audio_copy(self):
        args = [
            "-i",
            "input.mkv",
            "-codec:v:0",
            "h264_vaapi",
            "-codec:a:0",
            "copy",
            "-f",
            "hls",
            "out.m3u8",
        ]
        self.assertEqual(rffmpeg.classify_ffmpeg_job(args), "video_transcode")

    def test_full_transcode(self):
        args = [
            "-i",
            "input.mkv",
            "-codec:v:0",
            "libx264",
            "-codec:a:0",
            "aac",
            "-f",
            "hls",
            "out.m3u8",
        ]
        self.assertEqual(rffmpeg.classify_ffmpeg_job(args), "full_transcode")

    def test_audio_only_legacy_codec_option(self):
        args = ["-i", "input.flac", "-vn", "-acodec", "libmp3lame", "out.mp3"]
        self.assertEqual(rffmpeg.classify_ffmpeg_job(args), "audio_transcode")

    def test_decoder_codec_before_final_input_is_ignored(self):
        args = [
            "-c:v",
            "h264",
            "-i",
            "input.mkv",
            "-i",
            "external-audio.mka",
            "-codec:v:0",
            "copy",
            "-codec:a:0",
            "copy",
            "out.mkv",
        ]
        self.assertEqual(rffmpeg.classify_ffmpeg_job(args), "remux")

    def test_image_generation_is_unknown(self):
        args = [
            "-i",
            "input.mkv",
            "-vframes",
            "1",
            "-vf",
            "scale=320:-1",
            "-f",
            "image2",
            "thumbnail.jpg",
        ]
        self.assertEqual(rffmpeg.classify_ffmpeg_job(args), "unknown")

    def test_subtitle_extraction_is_unknown(self):
        args = [
            "-i",
            "input.mkv",
            "-map",
            "0:2",
            "-an",
            "-vn",
            "-c:s",
            "copy",
            "subtitle.srt",
        ]
        self.assertEqual(rffmpeg.classify_ffmpeg_job(args), "unknown")

    def test_missing_or_ambiguous_codec_information_is_unknown(self):
        self.assertEqual(rffmpeg.classify_ffmpeg_job(["-version"]), "unknown")
        self.assertEqual(
            rffmpeg.classify_ffmpeg_job(["-i", "input.mkv", "-codec", "copy", "out.mkv"]),
            "unknown",
        )

    def test_route_uses_detected_type_and_configured_policy(self):
        config = {
            "routing": {
                "remux": "local",
                "audio_transcode": "worker",
                "video_transcode": "worker",
                "full_transcode": "worker",
                "unknown": "local",
            }
        }
        job_type, route = rffmpeg.get_ffmpeg_route(
            config,
            ["-i", "input.mkv", "-codec:v:0", "copy", "-codec:a:0", "copy", "out.mkv"],
        )
        self.assertEqual((job_type, route), ("remux", "local"))

    def test_routing_defaults_preserve_worker_preference_and_local_fallback(self):
        with patch.dict(
            os.environ,
            {"RFFMPEG_CONFIG": "/tmp/rffmpeg-test-config-does-not-exist"},
        ):
            config = rffmpeg.load_config()
        self.assertEqual(config["routing"]["default"], "worker")
        self.assertEqual(config["routing"]["remux"], "worker")
        self.assertEqual(config["routing"]["unknown"], "worker")
        self.assertEqual(config["routing"]["worker_unavailable"], "local")

    def test_local_route_skips_worker_selection(self):
        config = {
            "routing": {
                "remux": "local",
                "worker_unavailable": "local",
            }
        }
        args = ["-i", "input.mkv", "-codec:v:0", "copy", "out.mkv"]

        with patch.object(rffmpeg, "cmd_name", "ffmpeg", create=True):
            with patch.object(rffmpeg, "hook_signals"), patch.object(
                rffmpeg, "setup_logging"
            ), patch.object(rffmpeg, "cleanup"), patch.object(
                rffmpeg, "get_target_host"
            ) as get_target_host, patch.object(
                rffmpeg,
                "run_local_ffmpeg",
                return_value=SimpleNamespace(returncode=0),
            ) as run_local_ffmpeg:
                with self.assertRaises(SystemExit) as result:
                    rffmpeg.run_ffmpeg(config, args)

        self.assertEqual(result.exception.code, 0)
        get_target_host.assert_not_called()
        run_local_ffmpeg.assert_called_once_with(config, None, args)

    def test_worker_route_can_fail_without_local_fallback(self):
        config = {
            "routing": {
                "remux": "worker",
                "worker_unavailable": "fail",
            }
        }
        args = ["-i", "input.mkv", "-codec:v:0", "copy", "out.mkv"]

        with patch.object(rffmpeg, "cmd_name", "ffmpeg", create=True):
            with patch.object(rffmpeg, "hook_signals"), patch.object(
                rffmpeg, "setup_logging"
            ), patch.object(rffmpeg, "cleanup"), patch.object(
                rffmpeg, "get_target_host", return_value=(None, None, None)
            ), patch.object(rffmpeg, "run_local_ffmpeg") as run_local_ffmpeg, patch.object(
                rffmpeg.log, "info"
            ), patch.object(rffmpeg.log, "error"):
                with self.assertRaises(SystemExit) as result:
                    rffmpeg.run_ffmpeg(config, args)

        self.assertEqual(result.exception.code, 1)
        run_local_ffmpeg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
