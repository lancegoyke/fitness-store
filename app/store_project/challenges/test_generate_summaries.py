from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from store_project.challenges.models import Challenge
from store_project.challenges.models import DifficultyLevel

COMMAND = "store_project.challenges.management.commands.generate_summaries"


class GenerateSummariesCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.missing = Challenge.objects.create(
            name="Missing Summary",
            description="Do 10 burpees and 20 squats.",
            slug="missing-summary",
            difficulty_level=DifficultyLevel.BEGINNER,
        )
        cls.has_summary = Challenge.objects.create(
            name="Has Summary",
            description="Run a mile.",
            summary="A quick mile run.",
            slug="has-summary",
            difficulty_level=DifficultyLevel.BEGINNER,
        )
        cls.empty_description = Challenge.objects.create(
            name="Empty Description",
            description="   ",
            slug="empty-description",
            difficulty_level=DifficultyLevel.BEGINNER,
        )

    @mock.patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"})
    @mock.patch(
        f"{COMMAND}.generate_challenge_summary", return_value="Generated blurb."
    )
    def test_fills_only_missing_summaries(self, mock_generate):
        out = StringIO()
        call_command("generate_summaries", stdout=out)

        self.missing.refresh_from_db()
        self.has_summary.refresh_from_db()

        self.assertEqual(self.missing.summary, "Generated blurb.")
        # Existing summary untouched, and not regenerated.
        self.assertEqual(self.has_summary.summary, "A quick mile run.")
        # Called once for the missing challenge, never for the empty-description one.
        mock_generate.assert_called_once_with("Do 10 burpees and 20 squats.")

    @mock.patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"})
    @mock.patch(
        f"{COMMAND}.generate_challenge_summary", return_value="Generated blurb."
    )
    def test_skips_empty_description(self, mock_generate):
        out = StringIO()
        call_command("generate_summaries", stdout=out)

        self.empty_description.refresh_from_db()
        self.assertEqual(self.empty_description.summary, "")
        self.assertIn("empty description", out.getvalue())

    @mock.patch(f"{COMMAND}.generate_challenge_summary")
    def test_dry_run_calls_no_api(self, mock_generate):
        out = StringIO()
        call_command("generate_summaries", "--dry-run", stdout=out)

        mock_generate.assert_not_called()
        self.missing.refresh_from_db()
        self.assertEqual(self.missing.summary, "")
        self.assertIn("DRY RUN", out.getvalue())
        self.assertIn("Missing Summary", out.getvalue())

    @mock.patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"})
    @mock.patch(f"{COMMAND}.generate_challenge_summary", return_value="Fresh.")
    def test_overwrite_regenerates_existing(self, mock_generate):
        out = StringIO()
        call_command("generate_summaries", "--overwrite", stdout=out)

        self.has_summary.refresh_from_db()
        self.assertEqual(self.has_summary.summary, "Fresh.")

    @mock.patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"})
    @mock.patch(f"{COMMAND}.generate_challenge_summary", return_value="Blurb.")
    def test_limit_caps_processing(self, mock_generate):
        # Add another missing-summary challenge so two are eligible.
        Challenge.objects.create(
            name="Another Missing",
            description="Plank for one minute.",
            slug="another-missing",
            difficulty_level=DifficultyLevel.BEGINNER,
        )
        out = StringIO()
        call_command("generate_summaries", "--limit", "1", stdout=out)

        self.assertEqual(mock_generate.call_count, 1)

    @mock.patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"})
    @mock.patch(f"{COMMAND}.generate_challenge_summary", return_value="Blurb.")
    def test_limit_does_not_count_blank_descriptions(self, mock_generate):
        # "Empty Description" sorts before "Missing Summary" alphabetically.
        # The blank row must not consume the limit, or --limit 1 would skip it
        # and never reach the one challenge that can actually be summarized.
        out = StringIO()
        call_command("generate_summaries", "--limit", "1", stdout=out)

        mock_generate.assert_called_once_with("Do 10 burpees and 20 squats.")
        self.missing.refresh_from_db()
        self.assertEqual(self.missing.summary, "Blurb.")

    @mock.patch.dict("os.environ", {}, clear=True)
    @mock.patch(f"{COMMAND}.generate_challenge_summary")
    def test_aborts_without_api_key(self, mock_generate):
        out = StringIO()
        err = StringIO()
        call_command("generate_summaries", stdout=out, stderr=err)

        mock_generate.assert_not_called()
        self.assertIn("GOOGLE_API_KEY not configured", err.getvalue())

    @mock.patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"})
    @mock.patch(
        f"{COMMAND}.generate_challenge_summary", side_effect=RuntimeError("quota")
    )
    def test_exits_nonzero_when_generation_fails(self, mock_generate):
        # A failed generation must surface as a non-zero exit (CommandError) so
        # automation can detect it, even though the command keeps going.
        out = StringIO()
        err = StringIO()
        with self.assertRaises(CommandError):
            call_command("generate_summaries", stdout=out, stderr=err)

        self.assertIn("failed", out.getvalue().lower())
