from datetime import timedelta

from django.test import TestCase
from django.urls import reverse

from store_project.challenges.models import DIFFICULTY_COLOR_MAPPING
from store_project.challenges.models import DIFFICULTY_ORDER
from store_project.challenges.models import VARIATION_NUMBER_PATTERN
from store_project.challenges.models import VARIATION_SUFFIX_PATTERN
from store_project.challenges.models import Challenge
from store_project.challenges.models import DifficultyLevel
from store_project.challenges.models import Record
from store_project.users.models import User


class ChallengeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="recorduser", email="recorduser@email.com", password="testpass123"
        )

        cls.adminuser = User.objects.create_superuser(
            username="adminuser", email="adminuser@email.com", password="testpass123"
        )

        cls.challenge = Challenge.objects.create(
            name="Test challenge",
            description="This is a hard workout",
            summary="Hard workout summary",
            slug="test-challenge",
            difficulty_level="beginner",
            tags=[],
        )

        cls.record = Record.objects.create(
            challenge=cls.challenge,
            user=cls.user,
            time_score=timedelta(hours=4, minutes=26, seconds=44),
        )

    def test_challenge_listing(self):
        self.assertEqual(f"{self.challenge.name}", "Test challenge")
        self.assertEqual(f"{self.challenge.description}", "This is a hard workout")
        self.assertEqual(f"{self.record.time_score}", "4:26:44")
        self.assertIsNotNone(self.record.user)
        if self.record.user:
            self.assertEqual(f"{self.record.user.username}", "recorduser")

    def test_challenge_base_name_property(self):
        """Test base_name property extracts base name correctly."""
        # Test regular challenge without variation
        regular_challenge = Challenge(name="Push-ups Challenge")
        self.assertEqual(regular_challenge.base_name, "Push-ups Challenge")

        # Test challenge with L1 variation
        l1_challenge = Challenge(name="Push-ups Challenge (L1)")
        self.assertEqual(l1_challenge.base_name, "Push-ups Challenge")

        # Test challenge with L2 variation
        l2_challenge = Challenge(name="Push-ups Challenge (L2)")
        self.assertEqual(l2_challenge.base_name, "Push-ups Challenge")

        # Test challenge with spaces before variation
        spaced_challenge = Challenge(name="Push-ups Challenge  (L3)")
        self.assertEqual(spaced_challenge.base_name, "Push-ups Challenge")

    def test_challenge_variation_number_property(self):
        """Test variation_number property extracts variation number correctly."""
        # Test regular challenge without variation
        regular_challenge = Challenge(name="Squats Challenge")
        self.assertIsNone(regular_challenge.variation_number)

        # Test challenge with L1 variation
        l1_challenge = Challenge(name="Squats Challenge (L1)")
        self.assertEqual(l1_challenge.variation_number, 1)

        # Test challenge with L5 variation
        l5_challenge = Challenge(name="Squats Challenge (L5)")
        self.assertEqual(l5_challenge.variation_number, 5)

    def test_challenge_is_variation_method(self):
        """Test is_variation method identifies variations correctly."""
        # Test regular challenge
        regular_challenge = Challenge(name="Burpees Challenge")
        self.assertFalse(regular_challenge.is_variation())

        # Test variation challenge
        variation_challenge = Challenge(name="Burpees Challenge (L1)")
        self.assertTrue(variation_challenge.is_variation())

    def test_challenge_queryset_grouped_method(self):
        """Test the grouped() queryset method groups challenges correctly."""
        # Create test challenges with variations
        Challenge.objects.create(
            name="Plank Challenge",
            description="Test",
            slug="plank-challenge",
            difficulty_level=DifficultyLevel.BEGINNER,
        )
        Challenge.objects.create(
            name="Plank Challenge (L1)",
            description="Test",
            slug="plank-challenge-l1",
            difficulty_level=DifficultyLevel.INTERMEDIATE,
        )
        Challenge.objects.create(
            name="Plank Challenge (L2)",
            description="Test",
            slug="plank-challenge-l2",
            difficulty_level=DifficultyLevel.ADVANCED,
        )
        Challenge.objects.create(
            name="Solo Challenge",
            description="Test",
            slug="solo-challenge",
            difficulty_level=DifficultyLevel.BEGINNER,
        )

        grouped = Challenge.objects.grouped()

        # Should have 3 groups: "Test challenge", "Plank Challenge", and "Solo Challenge"
        self.assertEqual(len(grouped), 3)
        self.assertIn("Test challenge", grouped)
        self.assertIn("Plank Challenge", grouped)
        self.assertIn("Solo Challenge", grouped)

        # Test challenge group should have 1 challenge
        self.assertEqual(len(grouped["Test challenge"]), 1)

        # Plank Challenge group should have 3 challenges
        self.assertEqual(len(grouped["Plank Challenge"]), 3)

        # Solo Challenge group should have 1 challenge
        self.assertEqual(len(grouped["Solo Challenge"]), 1)

        # Challenges in Plank Challenge group should be sorted by difficulty
        plank_challenges = grouped["Plank Challenge"]
        self.assertEqual(plank_challenges[0].difficulty_level, DifficultyLevel.BEGINNER)
        self.assertEqual(
            plank_challenges[1].difficulty_level, DifficultyLevel.INTERMEDIATE
        )
        self.assertEqual(plank_challenges[2].difficulty_level, DifficultyLevel.ADVANCED)

    def test_difficulty_constants(self):
        """Test that difficulty-related constants are properly defined."""
        # Test DIFFICULTY_ORDER constant
        self.assertEqual(DIFFICULTY_ORDER[DifficultyLevel.BEGINNER], 0)
        self.assertEqual(DIFFICULTY_ORDER[DifficultyLevel.INTERMEDIATE], 1)
        self.assertEqual(DIFFICULTY_ORDER[DifficultyLevel.ADVANCED], 2)

        # Test DIFFICULTY_COLOR_MAPPING constant
        self.assertEqual(DIFFICULTY_COLOR_MAPPING[DifficultyLevel.BEGINNER], "success")
        self.assertEqual(
            DIFFICULTY_COLOR_MAPPING[DifficultyLevel.INTERMEDIATE], "warning"
        )
        self.assertEqual(DIFFICULTY_COLOR_MAPPING[DifficultyLevel.ADVANCED], "danger")

    def test_variation_regex_patterns(self):
        """Test that variation regex patterns work correctly."""
        import re

        # Test VARIATION_SUFFIX_PATTERN
        test_names = [
            ("Challenge Name (L1)", "Challenge Name"),
            ("Challenge Name  (L2)", "Challenge Name"),
            ("Challenge Name", "Challenge Name"),
            ("Challenge (L10)", "Challenge"),
        ]

        for original, expected in test_names:
            result = re.sub(VARIATION_SUFFIX_PATTERN, "", original).strip()
            self.assertEqual(result, expected, f"Failed for: {original}")

        # Test VARIATION_NUMBER_PATTERN
        test_extractions = [
            ("Challenge (L1)", 1),
            ("Challenge (L5)", 5),
            ("Challenge (L10)", 10),
            ("Challenge", None),
            ("Challenge (Not a variation)", None),
        ]

        for name, expected in test_extractions:
            match = re.search(VARIATION_NUMBER_PATTERN, name)
            result = int(match[1]) if match else None
            self.assertEqual(result, expected, f"Failed for: {name}")

    def test_challenge_difficulty_color_property(self):
        """Test difficulty_color property returns correct colors."""
        beginner_challenge = Challenge(difficulty_level=DifficultyLevel.BEGINNER)
        self.assertEqual(beginner_challenge.difficulty_color, "success")

        intermediate_challenge = Challenge(
            difficulty_level=DifficultyLevel.INTERMEDIATE
        )
        self.assertEqual(intermediate_challenge.difficulty_color, "warning")

        advanced_challenge = Challenge(difficulty_level=DifficultyLevel.ADVANCED)
        self.assertEqual(advanced_challenge.difficulty_color, "danger")

    def test_challenge_list_view_for_logged_in_user(self):
        self.client.login(email="recorduser@email.com", password="testpass123")
        response = self.client.get(reverse("challenge_filtered_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test challenge")
        self.assertTemplateUsed(response, "challenges/challenge_filtered_list.html")
        self.assertContains(response, "Search")

    def test_challenge_list_view_for_logged_out_user(self):
        self.client.logout()
        response = self.client.get(reverse("challenge_filtered_list"))
        self.assertEqual(response.status_code, 302)

    def test_challenge_detail_view_for_logged_in_user(self):
        self.client.login(email="recorduser@email.com", password="testpass123")
        response = self.client.get(self.challenge.get_absolute_url())
        no_response = self.client.get("/challenges/12345/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(no_response.status_code, 404)
        self.assertContains(response, "Test challenge")
        self.assertContains(response, "This is a hard workout")
        self.assertContains(response, "recorduser")
        self.assertContains(response, "4:26:44")
        self.assertTemplateUsed(response, "challenges/challenge_detail.html")

    def test_challenge_detail_view_for_logged_in_superuser(self):
        self.client.logout()
        self.client.login(email="adminuser@email.com", password="testpass123")
        response = self.client.get(self.challenge.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test challenge")
        self.assertContains(response, "This is a hard workout")

    def test_challenge_detail_view_for_logged_out_user(self):
        self.client.logout()
        response = self.client.get(self.challenge.get_absolute_url())
        self.assertEqual(response.status_code, 302)

    def test_challenge_detail_view_record_create_form(self):
        # make sure logged in user can submit record
        self.client.login(email="recorduser@email.com", password="testpass123")
        response = self.client.post(
            self.challenge.get_absolute_url(),
            data={
                "time_score": "01",
                "notes": "I did it at the speed of light",
            },
        )
        response = self.client.get(self.challenge.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed("challenges/challenge_detail.html")
        self.assertContains(response, "recorduser")

        # make sure submitted record shows up afterwards
        # TODO

    def test_challenge_create_view_for_logged_in_adminuser(self):
        self.client.login(email="adminuser@email.com", password="testpass123")
        response = self.client.get(reverse("challenge_create"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "challenges/challenge_create.html")
        self.assertContains(response, "Create")

    def test_challenge_create_view_for_logged_in_user(self):
        self.client.login(email="recorduser@email.com", password="testpass123")
        # with self.assertRaises(PermissionDenied):
        response = self.client.get(reverse("challenge_create"))
        self.assertEqual(response.status_code, 403)

    def test_challenge_detail_paginates_records(self):
        # First check that we have the initial record from setUpTestData
        initial_count = Record.objects.filter(challenge=self.challenge).count()
        self.assertEqual(initial_count, 1)  # Should have 1 from setUpTestData

        # Create 75 MORE records to test pagination with 50 per page
        # This gives us 76 total (1 from setup + 75 new)
        for i in range(75):
            Record.objects.create(
                challenge=self.challenge,
                user=self.user,
                time_score=timedelta(seconds=i),
            )

        # Verify all records were created
        total_count = Record.objects.filter(challenge=self.challenge).count()
        self.assertEqual(total_count, 76)

        self.client.login(email="recorduser@email.com", password="testpass123")

        response = self.client.get(self.challenge.get_absolute_url())

        # Should have 50 items on page 1 and show we're on page 1 of 2
        self.assertEqual(len(response.context["page_obj"]), 50)
        self.assertContains(response, "Page 1 of 2")

        response = self.client.get(self.challenge.get_absolute_url() + "?page=2")
        self.assertEqual(
            len(response.context["page_obj"]), 26
        )  # 76 total - 50 on page 1 = 26
        self.assertContains(response, "Page 2 of 2")
