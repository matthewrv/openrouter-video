import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.error import URLError
from urllib.request import Request

from video_app.core import (
    ApiClient, ApiError, History, SafeRedirect, api_url, generation_options, make_payload, read_inputs,
)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.image = self.directory / "reference.png"
        self.image.write_bytes(b"test-image")
        self.text = self.directory / "script.txt"
        self.text.write_text("\ufeffПривет из файла!", encoding="utf-8")
        self.history = History(self.directory / "data")
        self.inputs, self.image_bytes = read_inputs(
            str(self.image), "file", str(self.text), "Черновик", "voice-123"
        )

    def tearDown(self):
        self.history.close()
        self.temporary.cleanup()

    def new_job(self):
        return self.history.create(self.inputs, self.image_bytes)

    def test_payload_matches_api_contract(self):
        payload = make_payload(self.inputs, self.image_bytes)
        self.assertEqual(payload["model"], "heygen/avatar-iv")
        self.assertEqual(payload["prompt"], "Привет из файла!")
        self.assertEqual(payload["provider"]["options"]["heygen"]["voice_id"], "voice-123")
        self.assertTrue(payload["input_references"][0]["image_url"]["url"].startswith(
            "data:image/png;base64,"
        ))

    def test_inline_text_and_required_fields(self):
        inputs, _ = read_inputs(str(self.image), "inline", "", "Текст", "voice")
        self.assertEqual(inputs["prompt"], "Текст")
        for prompt, voice in (("", "voice"), ("Текст", "")):
            with self.assertRaises(ValueError):
                read_inputs(str(self.image), "inline", "", prompt, voice)

    def test_payload_omits_unset_generation_options(self):
        inputs = dict(self.inputs, duration=None, resolution=None, size=None)
        payload = make_payload(inputs, self.image_bytes)
        for field in ("duration", "resolution", "size", "resolution_mode", "image_size"):
            self.assertNotIn(field, payload)

    def test_payload_includes_integer_duration_and_standard_resolution(self):
        for resolution in ("720p", "1080p"):
            with self.subTest(resolution=resolution):
                inputs = dict(self.inputs, duration=5, resolution=resolution)
                payload = make_payload(inputs, self.image_bytes)
                self.assertEqual(payload["duration"], 5)
                self.assertIs(type(payload["duration"]), int)
                self.assertEqual(payload["resolution"], resolution)
                self.assertNotIn("size", payload)

    def test_payload_includes_size_without_ui_metadata(self):
        inputs = dict(self.inputs, size="1024x1024", resolution_mode="original", image_size="1024x1024")
        payload = make_payload(inputs, self.image_bytes)
        self.assertEqual(payload["size"], "1024x1024")
        self.assertNotIn("resolution", payload)
        self.assertNotIn("resolution_mode", payload)
        self.assertNotIn("image_size", payload)

    def test_generation_options_validate_integer_and_dimensions(self):
        invalid = (
            {"duration": 0}, {"duration": -1}, {"duration": True},
            {"duration": 1.5}, {"duration": "5"}, {"resolution": "4k"},
            {"size": "0x720"}, {"size": "1280x-1"}, {"size": "1280×720"},
            {"size": "1280x720extra"}, {"size": 1024},
            {"resolution": "720p", "size": "1280x720"},
        )
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                generation_options(**options)
        self.assertEqual(generation_options(), {})
        self.assertEqual(generation_options(duration=1, size="100x200"), {
            "duration": 1, "size": "100x200",
        })

    def test_snapshot_and_input_immutability(self):
        local_id = self.new_job()
        self.image.write_bytes(b"changed")
        self.text.write_text("Изменён", encoding="utf-8")
        job = self.history.get(local_id)
        self.assertEqual(job["inputs"]["prompt"], "Привет из файла!")
        self.assertEqual(job["image"], b"test-image")
        with self.assertRaises(ValueError):
            self.history.update(local_id, inputs={})
        with self.assertRaises(sqlite3.IntegrityError):
            self.history.connection.execute(
                "UPDATE jobs SET inputs='{}' WHERE local_id=?", (local_id,)
            )

    def test_completed_keeps_polling_until_signed_urls(self):
        local_id = self.new_job()
        self.history.apply_response(local_id, {
            "id": "gen-123", "status": "pending",
            "polling_url": "/api/v1/videos/gen-123",
        }, now=100)
        self.history.apply_response(local_id, {
            "status": "completed", "unsigned_urls": ["/api/v1/videos/gen-123/content"],
        }, now=200)
        completed = self.history.get(local_id)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["next_poll"], 230)
        self.history.apply_response(local_id, {
            "status": "completed", "urls": ["https://cdn.example/video.mp4"],
        }, now=240)
        final = self.history.get(local_id)
        self.assertEqual(final["response"]["unsigned_urls"], ["/api/v1/videos/gen-123/content"])
        self.assertEqual(final["response"]["urls"], ["https://cdn.example/video.mp4"])
        self.assertIsNone(final["next_poll"])

    def test_completed_polling_has_deadline_and_manual_can_update(self):
        local_id = self.new_job()
        self.history.apply_response(local_id, {"id": "gen-123", "status": "completed"}, now=100)
        self.history.apply_response(local_id, {"status": "completed"}, now=3700)
        self.assertIsNone(self.history.get(local_id)["next_poll"])
        self.history.apply_response(local_id, {
            "status": "completed", "urls": ["https://cdn.example/video.mp4"],
        }, now=3800)
        self.assertEqual(len(self.history.get(local_id)["response"]["urls"]), 1)

    def test_failed_job_stops_polling(self):
        local_id = self.new_job()
        self.history.apply_response(local_id, {"id": "gen-123", "status": "pending"}, now=100)
        self.history.apply_response(local_id, {
            "status": "failed", "error": {"message": "Generation failed"},
        }, now=130)
        job = self.history.get(local_id)
        self.assertIsNone(job["next_poll"])
        self.assertIn("Generation failed", job["error"])

    def test_poll_error_preserves_results_and_retries(self):
        local_id = self.new_job()
        self.history.apply_response(local_id, {
            "id": "gen-123", "status": "completed",
            "unsigned_urls": ["https://openrouter.ai/api/v1/videos/gen-123/content"],
        }, now=100)
        self.history.poll_error(local_id, "Network unavailable", now=130)
        job = self.history.get(local_id)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(len(job["response"]["unsigned_urls"]), 1)
        self.assertGreater(job["next_poll"], 130)

    def test_restart_marks_unconfirmed_post_but_resumes_pending(self):
        uncertain_id = self.new_job()
        pending_id = self.new_job()
        self.history.apply_response(pending_id, {"id": "gen-123", "status": "pending"}, now=100)
        self.history.close()
        self.history = History(self.directory / "data")
        self.assertEqual(self.history.get(uncertain_id)["status"], "uncertain")
        self.assertIsNone(self.history.get(uncertain_id)["next_poll"])
        self.assertEqual(self.history.get(pending_id)["status"], "pending")
        self.assertEqual(len(self.history.due()), 1)

    def test_api_url_does_not_leak_token(self):
        self.assertEqual(api_url("/api/v1/videos/gen-123"), "https://openrouter.ai/api/v1/videos/gen-123")
        for url in (
            "https://evil.example/api/v1/videos/gen-123",
            "https://openrouter.ai.evil.example/api/v1/videos/gen-123",
            "http://openrouter.ai/api/v1/videos/gen-123",
            "https://secret@openrouter.ai/api/v1/videos/gen-123",
            "https://openrouter.ai:444/api/v1/videos/gen-123",
            "https://openrouter.ai/api/v1/chat/completions",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                api_url(url)

    def test_redirect_removes_authorization_for_external_host(self):
        request = Request(
            "https://openrouter.ai/api/v1/videos/gen-123/content",
            headers={"Authorization": "Bearer secret"},
        )
        redirected = SafeRedirect().redirect_request(
            request, None, 302, "", {}, "https://cdn.example/video.mp4"
        )
        self.assertIsNone(redirected.get_header("Authorization"))
        post = Request("https://openrouter.ai/api/v1/videos", data=b"{}")
        with self.assertRaises(ValueError):
            SafeRedirect().redirect_request(post, None, 307, "", {}, "https://openrouter.ai/other")

    def test_post_is_sent_only_once_on_network_failure(self):
        client = ApiClient("secret")
        client.opener = Mock()
        client.opener.open.side_effect = URLError("connection reset")
        with self.assertRaises(ApiError):
            client.request("", make_payload(self.inputs, self.image_bytes))
        self.assertEqual(client.opener.open.call_count, 1)
        request = client.opener.open.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")

    def test_download_auth_and_atomic_output(self):
        client = ApiClient("secret")
        client.opener = Mock()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = [b"video", b""]
        client.opener.open.return_value = response
        target = self.directory / "result.mp4"
        client.download("/api/v1/videos/gen-123/content", target, authenticated=True)
        self.assertEqual(target.read_bytes(), b"video")
        self.assertEqual(
            client.opener.open.call_args[0][0].get_header("Authorization"), "Bearer secret"
        )
        self.assertFalse(list(self.directory.glob("*.part")))

    def test_invalid_response_does_not_destroy_history(self):
        local_id = self.new_job()
        with self.assertRaises(ValueError):
            self.history.apply_response(local_id, {"status": "completed", "urls": "not-an-array"})
        self.assertEqual(self.history.get(local_id)["status"], "submitting")

    def test_null_link_arrays_are_accepted(self):
        local_id = self.new_job()
        self.history.apply_response(local_id, {
            "id": "gen-123", "status": "pending", "urls": None, "unsigned_urls": None,
        })
        self.assertEqual(self.history.get(local_id)["response"]["urls"], [])


if __name__ == "__main__":
    unittest.main()
