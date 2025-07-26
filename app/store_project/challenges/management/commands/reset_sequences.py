from django.core.management.base import BaseCommand
from django.db import DEFAULT_DB_ALIAS
from django.db import connections
from store_project.challenges.models import Challenge
from store_project.challenges.models import Record


class Command(BaseCommand):
    help = (
        "Reset database auto-increment sequences for models with integer primary keys"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be reset without actually resetting sequences",
        )
        parser.add_argument(
            "--models",
            nargs="+",
            choices=["challenge", "record", "all"],
            default=["all"],
            help="Specify which models to reset (default: all)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        models_to_reset = options["models"]

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No sequences will be reset")
            )

        # Only include models with auto-increment primary keys
        # User model uses UUID primary key, so it doesn't have a sequence
        model_map = {
            "challenge": Challenge,
            "record": Record,
        }

        if "all" in models_to_reset:
            models = list(model_map.values())
            self.stdout.write(
                "Processing models with auto-increment IDs: Challenge, Record"
            )
        else:
            # Filter out any models that don't have sequences
            valid_models = [name for name in models_to_reset if name in model_map]
            if not valid_models:
                self.stdout.write(
                    self.style.ERROR(
                        "No valid models specified. Valid options: challenge, record, all"
                    )
                )
                return
            models = [model_map[name] for name in valid_models]
            self.stdout.write(f"Processing models: {', '.join(valid_models)}")

        self.stdout.write("")

        # Show current sequence diagnostics
        self._show_sequence_diagnostics(models)

        if not dry_run:
            self.stdout.write("")
            self.stdout.write("Resetting sequences...")
            self._reset_sequences(models)
            self.stdout.write(self.style.SUCCESS("✓ Sequences reset successfully!"))

            # Show updated diagnostics
            self.stdout.write("")
            self.stdout.write("Updated sequence values:")
            self._show_sequence_diagnostics(models)
        else:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "DRY RUN COMPLETE - Run without --dry-run to apply changes"
                )
            )

    def _show_sequence_diagnostics(self, models):
        """Show current sequence values vs maximum IDs in tables."""
        connection = connections[DEFAULT_DB_ALIAS]

        for model in models:
            table_name = model._meta.db_table

            with connection.cursor() as cursor:
                # Get the sequence name using PostgreSQL's built-in function
                cursor.execute(f"SELECT pg_get_serial_sequence('{table_name}', 'id');")
                sequence_result = cursor.fetchone()

                if not sequence_result or not sequence_result[0]:
                    self.stdout.write(
                        f"⚠ {model.__name__}: no sequence found for table '{table_name}'"
                    )
                    continue

                sequence_name = sequence_result[0]

                # Get current sequence value
                # Use last_value which shows the actual current value of the sequence
                cursor.execute(f"SELECT last_value FROM {sequence_name};")
                current_seq = cursor.fetchone()[0]

                # Get maximum ID in table
                cursor.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table_name};")
                max_id = cursor.fetchone()[0]

                # Determine status
                # Sequence should be >= max_id (or > max_id for next insert to work)
                next_val_ok = current_seq > max_id
                status_icon = "✓" if next_val_ok else "⚠"
                status_color = self.style.SUCCESS if next_val_ok else self.style.ERROR

                self.stdout.write(
                    f"{status_icon} {model.__name__}: "
                    f"sequence={current_seq}, max_id={max_id}, next_will_be={current_seq + 1} "
                    f"({status_color('OK' if next_val_ok else 'NEEDS RESET')})"
                )

    def _reset_sequences(self, models):
        """Reset database sequences for the specified models."""
        connection = connections[DEFAULT_DB_ALIAS]

        with connection.cursor() as cursor:
            for model in models:
                table_name = model._meta.db_table

                # Get the sequence name
                cursor.execute(f"SELECT pg_get_serial_sequence('{table_name}', 'id');")
                sequence_result = cursor.fetchone()

                if not sequence_result or not sequence_result[0]:
                    self.stdout.write(f"⚠ Skipping {model.__name__}: no sequence found")
                    continue

                sequence_name = sequence_result[0]

                # Get max ID from table
                cursor.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table_name};")
                max_id = cursor.fetchone()[0]

                # Set sequence to max_id + 1 so next insert gets max_id + 1
                new_value = max_id + 1
                sql = f"SELECT setval('{sequence_name}', {new_value});"

                self.stdout.write(f"Executing: {sql}")
                cursor.execute(sql)

                # Verify the change
                cursor.execute(f"SELECT last_value FROM {sequence_name};")
                updated_seq = cursor.fetchone()[0]
                self.stdout.write(
                    f"✓ {model.__name__}: sequence updated to {updated_seq}"
                )
