from datetime import timedelta

from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from store_project.challenges.filters import ChallengeFilter
from store_project.challenges.models import DIFFICULTY_COLOR_MAPPING
from store_project.challenges.models import DIFFICULTY_ORDER
from store_project.challenges.models import VARIATION_NUMBER_PATTERN
from store_project.challenges.models import VARIATION_SUFFIX_PATTERN
from store_project.challenges.models import Challenge
from store_project.challenges.models import ChallengeTag
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
        response = self.client.get(reverse("challenges:challenge_filtered_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test challenge")
        self.assertTemplateUsed(response, "challenges/challenge_filtered_list.html")
        self.assertContains(response, "Search")

    def test_challenge_list_view_for_logged_out_user(self):
        self.client.logout()
        response = self.client.get(reverse("challenges:challenge_filtered_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test challenge")
        self.assertTemplateUsed(response, "challenges/challenge_filtered_list.html")

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
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/accounts/google/login/")

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
        response = self.client.get(reverse("challenges:challenge_create"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "challenges/challenge_create.html")
        self.assertContains(response, "Create")

    def test_challenge_create_view_for_logged_in_user(self):
        self.client.login(email="recorduser@email.com", password="testpass123")
        # with self.assertRaises(PermissionDenied):
        response = self.client.get(reverse("challenges:challenge_create"))
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


class ChallengeFilterOrderingTests(TestCase):
    """Test cases for ChallengeFilter ordering functionality."""

    @classmethod
    def setUpTestData(cls):
        """Set up test data with challenges and records for ordering tests."""
        cls.user = User.objects.create_user(
            username="testuser", email="testuser@test.com", password="testpass123"
        )

        # Create challenges with predictable names for alphabetical ordering
        cls.challenge_alpha = Challenge.objects.create(
            name="Alpha Challenge",
            description="First challenge alphabetically",
            slug="alpha-challenge",
            difficulty_level=DifficultyLevel.BEGINNER,
        )

        cls.challenge_beta = Challenge.objects.create(
            name="Beta Challenge",
            description="Second challenge alphabetically",
            slug="beta-challenge",
            difficulty_level=DifficultyLevel.INTERMEDIATE,
        )

        cls.challenge_gamma = Challenge.objects.create(
            name="Gamma Challenge",
            description="Third challenge alphabetically",
            slug="gamma-challenge",
            difficulty_level=DifficultyLevel.ADVANCED,
        )

        # Create records to test popularity ordering
        # Use a fixed reference time to avoid race conditions
        cls.reference_time = timezone.now().replace(
            hour=12, minute=0, second=0, microsecond=0
        )

        # Mock timezone.now to control auto_now_add dates
        from unittest.mock import patch

        # Beta Challenge: 5 records in last month (most popular)
        for i in range(5):
            target_date = cls.reference_time - timedelta(days=i + 1)
            with patch("django.utils.timezone.now", return_value=target_date):
                Record.objects.create(
                    challenge=cls.challenge_beta,
                    user=cls.user,
                    time_score=timedelta(minutes=10 + i),
                )

        # Gamma Challenge: 3 records in last month (second most popular)
        for i in range(3):
            target_date = cls.reference_time - timedelta(days=i + 2)
            with patch("django.utils.timezone.now", return_value=target_date):
                Record.objects.create(
                    challenge=cls.challenge_gamma,
                    user=cls.user,
                    time_score=timedelta(minutes=15 + i),
                )

        # Alpha Challenge: 1 record in last month (least popular)
        target_date = cls.reference_time - timedelta(days=5)
        with patch("django.utils.timezone.now", return_value=target_date):
            Record.objects.create(
                challenge=cls.challenge_alpha,
                user=cls.user,
                time_score=timedelta(minutes=20),
            )

        # Add old records (> 30 days) that should not count for popularity
        target_date = cls.reference_time - timedelta(days=35)
        with patch("django.utils.timezone.now", return_value=target_date):
            Record.objects.create(
                challenge=cls.challenge_alpha,
                user=cls.user,
                time_score=timedelta(minutes=25),
            )

    def test_default_ordering_is_popularity(self):
        """Test that default ordering (no parameters) sorts by popularity."""
        from unittest.mock import patch

        # Mock timezone.now to use our reference time
        with patch(
            "store_project.challenges.filters.timezone.now",
            return_value=self.reference_time,
        ):
            data = QueryDict("")  # Empty - simulates default page load
            # Use only our test challenges, not all challenges in the database
            test_queryset = Challenge.objects.filter(
                pk__in=[
                    self.challenge_alpha.pk,
                    self.challenge_beta.pk,
                    self.challenge_gamma.pk,
                ]
            )
            filter_obj = ChallengeFilter(data, queryset=test_queryset)

            challenges_list = list(filter_obj.qs)

            # Should be ordered by popularity (Beta=5, Gamma=3, Alpha=1)
            self.assertEqual(challenges_list[0], self.challenge_beta)
            self.assertEqual(challenges_list[1], self.challenge_gamma)
            self.assertEqual(challenges_list[2], self.challenge_alpha)

            # Verify record_count annotation is present
            self.assertTrue(hasattr(challenges_list[0], "record_count"))
            self.assertEqual(challenges_list[0].record_count, 5)
            self.assertEqual(challenges_list[1].record_count, 3)
            self.assertEqual(challenges_list[2].record_count, 1)

    def test_explicit_popularity_ordering(self):
        """Test explicit popularity ordering parameter."""
        data = QueryDict("ordering=popularity")
        test_queryset = Challenge.objects.filter(
            pk__in=[
                self.challenge_alpha.pk,
                self.challenge_beta.pk,
                self.challenge_gamma.pk,
            ]
        )
        filter_obj = ChallengeFilter(data, queryset=test_queryset)

        challenges_list = list(filter_obj.qs)

        # Should be ordered by popularity (Beta=5, Gamma=3, Alpha=1)
        self.assertEqual(challenges_list[0], self.challenge_beta)
        self.assertEqual(challenges_list[1], self.challenge_gamma)
        self.assertEqual(challenges_list[2], self.challenge_alpha)

    def test_alphabetical_ordering(self):
        """Test explicit alphabetical ordering."""
        data = QueryDict("ordering=name")
        test_queryset = Challenge.objects.filter(
            pk__in=[
                self.challenge_alpha.pk,
                self.challenge_beta.pk,
                self.challenge_gamma.pk,
            ]
        )
        filter_obj = ChallengeFilter(data, queryset=test_queryset)

        challenges_list = list(filter_obj.qs)

        # Should be ordered alphabetically (Alpha, Beta, Gamma)
        self.assertEqual(challenges_list[0], self.challenge_alpha)
        self.assertEqual(challenges_list[1], self.challenge_beta)
        self.assertEqual(challenges_list[2], self.challenge_gamma)

    def test_date_ordering_newest_first(self):
        """Test ordering by newest creation date first."""
        data = QueryDict("ordering=-date_created")
        test_queryset = Challenge.objects.filter(
            pk__in=[
                self.challenge_alpha.pk,
                self.challenge_beta.pk,
                self.challenge_gamma.pk,
            ]
        )
        filter_obj = ChallengeFilter(data, queryset=test_queryset)

        challenges_list = list(filter_obj.qs)

        # Should be ordered by creation date (newest first)
        # Gamma was created last, then Beta, then Alpha
        self.assertEqual(challenges_list[0], self.challenge_gamma)
        self.assertEqual(challenges_list[1], self.challenge_beta)
        self.assertEqual(challenges_list[2], self.challenge_alpha)

    def test_date_ordering_oldest_first(self):
        """Test ordering by oldest creation date first."""
        data = QueryDict("ordering=date_created")
        test_queryset = Challenge.objects.filter(
            pk__in=[
                self.challenge_alpha.pk,
                self.challenge_beta.pk,
                self.challenge_gamma.pk,
            ]
        )
        filter_obj = ChallengeFilter(data, queryset=test_queryset)

        challenges_list = list(filter_obj.qs)

        # Should be ordered by creation date (oldest first)
        # Alpha was created first, then Beta, then Gamma
        self.assertEqual(challenges_list[0], self.challenge_alpha)
        self.assertEqual(challenges_list[1], self.challenge_beta)
        self.assertEqual(challenges_list[2], self.challenge_gamma)

    def test_popularity_uses_last_month_only(self):
        """Test that popularity ordering only counts records from last 30 days."""
        from unittest.mock import patch

        # Mock timezone.now to use our reference time
        with patch(
            "store_project.challenges.filters.timezone.now",
            return_value=self.reference_time,
        ):
            # The old record (35 days old) should not affect Alpha's popularity
            data = QueryDict("ordering=popularity")
            test_queryset = Challenge.objects.filter(
                pk__in=[
                    self.challenge_alpha.pk,
                    self.challenge_beta.pk,
                    self.challenge_gamma.pk,
                ]
            )
            filter_obj = ChallengeFilter(data, queryset=test_queryset)

            challenges_list = list(filter_obj.qs)

            # Alpha should have record_count=1 (not 2, because old record is excluded)
            alpha_challenge = next(
                c for c in challenges_list if c == self.challenge_alpha
            )
            self.assertEqual(alpha_challenge.record_count, 1)

    def test_popularity_secondary_alphabetical_ordering(self):
        """Test that challenges with same popularity are ordered alphabetically."""
        # Create two challenges with same number of records
        challenge_delta = Challenge.objects.create(
            name="Delta Challenge",
            description="Test secondary ordering",
            slug="delta-challenge",
            difficulty_level=DifficultyLevel.BEGINNER,
        )

        challenge_charlie = Challenge.objects.create(
            name="Charlie Challenge",
            description="Test secondary ordering",
            slug="charlie-challenge",
            difficulty_level=DifficultyLevel.BEGINNER,
        )

        # Give both 2 records (same popularity)
        now = timezone.now()
        for challenge in [challenge_delta, challenge_charlie]:
            for i in range(2):
                Record.objects.create(
                    challenge=challenge,
                    user=self.user,
                    time_score=timedelta(minutes=10 + i),
                    date_recorded=now - timedelta(days=i + 1),
                )

        data = QueryDict("ordering=popularity")
        # Include the new challenges in our test queryset
        test_queryset = Challenge.objects.filter(
            pk__in=[
                self.challenge_alpha.pk,
                self.challenge_beta.pk,
                self.challenge_gamma.pk,
                challenge_delta.pk,
                challenge_charlie.pk,
            ]
        )
        filter_obj = ChallengeFilter(data, queryset=test_queryset)

        challenges_list = list(filter_obj.qs)

        # Find Charlie and Delta in the results
        charlie_idx = next(
            i for i, c in enumerate(challenges_list) if c == challenge_charlie
        )
        delta_idx = next(
            i for i, c in enumerate(challenges_list) if c == challenge_delta
        )

        # Charlie should come before Delta alphabetically (both have 2 records)
        self.assertLess(charlie_idx, delta_idx)

        # Both should have same record count
        self.assertEqual(challenges_list[charlie_idx].record_count, 2)
        self.assertEqual(challenges_list[delta_idx].record_count, 2)

    def test_filter_form_initial_value(self):
        """Test that the filter form has correct initial value."""
        data = QueryDict("")
        filter_obj = ChallengeFilter(data, queryset=Challenge.objects.all())

        # The initial value should be set to 'popularity'
        ordering_field = filter_obj.filters["ordering"]
        self.assertEqual(ordering_field.extra.get("initial"), "popularity")

    def test_filter_form_choices(self):
        """Test that filter form has correct choices in correct order."""
        data = QueryDict("")
        filter_obj = ChallengeFilter(data, queryset=Challenge.objects.all())

        ordering_field = filter_obj.filters["ordering"]
        choices = ordering_field.extra.get("choices")

        expected_choices = [
            ("popularity", "By Popularity"),
            ("name", "Alphabetical"),
            ("-date_created", "Newest First"),
            ("date_created", "Oldest First"),
        ]

        self.assertEqual(list(choices), expected_choices)

    def test_grouped_method_preserves_ordering(self):
        """Test that the grouped() method preserves queryset ordering."""
        # Test with alphabetical ordering
        data = QueryDict("ordering=name")
        test_queryset = Challenge.objects.filter(
            pk__in=[
                self.challenge_alpha.pk,
                self.challenge_beta.pk,
                self.challenge_gamma.pk,
            ]
        )
        filter_obj = ChallengeFilter(data, queryset=test_queryset)
        grouped = filter_obj.qs.grouped()

        # Get the order of base names
        base_names = list(grouped.keys())
        self.assertEqual(
            base_names, ["Alpha Challenge", "Beta Challenge", "Gamma Challenge"]
        )

        # Test with popularity ordering
        data = QueryDict("ordering=popularity")
        test_queryset = Challenge.objects.filter(
            pk__in=[
                self.challenge_alpha.pk,
                self.challenge_beta.pk,
                self.challenge_gamma.pk,
            ]
        )
        filter_obj = ChallengeFilter(data, queryset=test_queryset)
        grouped = filter_obj.qs.grouped()

        # Get the order of base names
        base_names = list(grouped.keys())
        self.assertEqual(
            base_names, ["Beta Challenge", "Gamma Challenge", "Alpha Challenge"]
        )


class ChallengeURLLoadingTests(TestCase):
    """Test that all Challenge app URLs load correctly."""

    @classmethod
    def setUpTestData(cls):
        # Create users
        cls.user = User.objects.create_user(
            username="testuser", email="testuser@example.com", password="testpass123"
        )
        cls.admin_user = User.objects.create_superuser(
            username="adminuser", email="admin@example.com", password="testpass123"
        )

        # Create challenge tag
        cls.challenge_tag = ChallengeTag.objects.create(
            name="Test Tag", slug="test-tag"
        )

        # Create challenge
        cls.challenge = Challenge.objects.create(
            name="Test Challenge",
            description="Test challenge description",
            slug="test-challenge",
            difficulty_level=DifficultyLevel.BEGINNER,
        )
        cls.challenge.challenge_tags.add(cls.challenge_tag)

    def test_challenge_filtered_list_url_loads_for_authenticated_user(self):
        """Test that the main challenge list URL loads for authenticated users."""
        self.client.login(email="testuser@example.com", password="testpass123")
        response = self.client.get(reverse("challenges:challenge_filtered_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pick a Challenge")
        self.assertContains(response, "Test Challenge")

    def test_challenge_filtered_list_url_loads_for_unauthenticated_user(self):
        """Test that the main challenge list URL loads for unauthenticated users."""
        response = self.client.get(reverse("challenges:challenge_filtered_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pick a Challenge")
        self.assertContains(response, "Test Challenge")

    def test_challenge_detail_url_loads_for_authenticated_user(self):
        """Test that challenge detail URL loads for authenticated users."""
        self.client.login(email="testuser@example.com", password="testpass123")
        response = self.client.get(
            reverse("challenges:challenge_detail", kwargs={"slug": self.challenge.slug})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Challenge")
        self.assertContains(response, "Test challenge description")

    def test_challenge_detail_url_redirects_for_unauthenticated_user(self):
        """Unauthenticated users should still access detail page but see login form."""
        response = self.client.get(
            reverse("challenges:challenge_detail", kwargs={"slug": self.challenge.slug})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/accounts/google/login/")

    def test_challenge_create_url_loads_for_admin_user(self):
        """Test that challenge create URL loads for admin users with permissions."""
        self.client.login(email="admin@example.com", password="testpass123")
        response = self.client.get(reverse("challenges:challenge_create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Challenge")

    def test_challenge_create_url_forbidden_for_regular_user(self):
        """Test that challenge create URL is forbidden for regular users."""
        self.client.login(email="testuser@example.com", password="testpass123")
        response = self.client.get(reverse("challenges:challenge_create"))
        self.assertEqual(response.status_code, 403)

    def test_challenge_create_url_redirects_for_unauthenticated_user(self):
        """Test that challenge create URL redirects unauthenticated users."""
        response = self.client.get(reverse("challenges:challenge_create"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_challenge_tag_filtered_list_url_loads_for_authenticated_user(self):
        """Test that tag-filtered challenge list URL loads for authenticated users."""
        self.client.login(email="testuser@example.com", password="testpass123")
        response = self.client.get(
            reverse(
                "challenges:challenge_tag_filtered_list",
                kwargs={"slug": self.challenge_tag.slug},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pick a Challenge")
        self.assertContains(response, "Test Challenge")

    def test_challenge_tag_filtered_list_url_loads_for_unauthenticated_user(self):
        """Test that tag-filtered challenge list URL loads for unauthenticated users."""
        response = self.client.get(
            reverse(
                "challenges:challenge_tag_filtered_list",
                kwargs={"slug": self.challenge_tag.slug},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pick a Challenge")
        self.assertContains(response, "Test Challenge")

    def test_challenge_tag_list_url_loads_for_authenticated_user(self):
        """Test that the tag list URL loads for authenticated users."""
        self.client.login(email="testuser@example.com", password="testpass123")
        response = self.client.get(reverse("challenges:challenge_tag_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pick a Challenge")

    def test_challenge_tag_list_url_loads_for_unauthenticated_user(self):
        """Test that the tag list URL loads for unauthenticated users."""
        response = self.client.get(reverse("challenges:challenge_tag_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pick a Challenge")

    def test_nonexistent_challenge_detail_returns_404(self):
        """Test that accessing a nonexistent challenge returns 404."""
        self.client.login(email="testuser@example.com", password="testpass123")
        response = self.client.get(
            reverse("challenges:challenge_detail", kwargs={"slug": "nonexistent-slug"})
        )
        self.assertEqual(response.status_code, 404)

    def test_nonexistent_tag_filtered_list_returns_empty_results(self):
        """Test that accessing a nonexistent tag returns empty results."""
        self.client.login(email="testuser@example.com", password="testpass123")
        response = self.client.get(
            reverse(
                "challenges:challenge_tag_filtered_list",
                kwargs={"slug": "nonexistent-tag"},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No challenges found")
