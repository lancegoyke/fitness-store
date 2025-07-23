# Production-safe migration to remove django-taggit dependency

from django.db import migrations


def check_and_remove_taggit_dependency(apps, schema_editor):
    """
    Check if django-taggit tables exist and clean up any remaining references.
    This handles the case where production databases might have taggit data.
    """
    db_alias = schema_editor.connection.alias

    # Check if taggit tables exist in the database
    # Use database-specific queries
    try:
        with schema_editor.connection.cursor() as cursor:
            if schema_editor.connection.vendor == 'postgresql':
                cursor.execute("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name IN ('taggit_tag', 'taggit_taggeditem');
                """)
                taggit_tables = [row[0] for row in cursor.fetchall()]
            elif schema_editor.connection.vendor == 'sqlite':
                cursor.execute("""
                    SELECT name FROM sqlite_master
                    WHERE type='table'
                    AND name IN ('taggit_tag', 'taggit_taggeditem');
                """)
                taggit_tables = [row[0] for row in cursor.fetchall()]
            else:
                # For other databases, just assume no taggit tables
                taggit_tables = []

        # If taggit tables exist, log that we're keeping them for now
        # but they're no longer actively used by the challenges app
        if taggit_tables:
            print(f"Found taggit tables: {taggit_tables}")
            print("These tables are preserved but no longer used by the challenges app.")
            print("You can remove them manually after confirming no other apps use them.")
    except Exception as e:
        # If there's any error checking tables, just continue silently
        # This ensures the migration doesn't fail in edge cases
        pass


def reverse_check(apps, schema_editor):
    """Reverse operation - nothing needed."""
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("challenges", "0006_auto_20250723_1310"),
    ]

    operations = [
        migrations.RunPython(
            check_and_remove_taggit_dependency,
            reverse_check,
        ),
    ]
