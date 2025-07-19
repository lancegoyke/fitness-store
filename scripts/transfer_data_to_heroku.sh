#!/bin/bash

# Script to securely transfer data files to Heroku for import
# Usage: ./scripts/transfer_data_to_heroku.sh

set -e

echo "🔐 Securely transferring production data to Heroku..."

# Check if data files exist
if [ ! -d "data-import" ]; then
    echo "❌ Error: data-import directory not found!"
    echo "Make sure your JSON files are in the data-import/ directory"
    exit 1
fi

# Check required files
required_files=(
    "production-users.json"
    "production-tags.json"
    "production-challenges.json"
    "production-records.json"
    "production-tagged_items.json"
)

echo "📋 Checking required files..."
missing_files=()
for file in "${required_files[@]}"; do
    if [ ! -f "data-import/$file" ]; then
        missing_files+=("$file")
    else
        echo "  ✓ $file"
    fi
done

if [ ${#missing_files[@]} -ne 0 ]; then
    echo "❌ Missing files:"
    for file in "${missing_files[@]}"; do
        echo "  ❌ $file"
    done
    exit 1
fi

echo ""
echo "🗜️  Compressing data files..."
tar -czf /tmp/production-data.tar.gz data-import/
echo "  ✓ Created /tmp/production-data.tar.gz"

echo ""
echo "📤 Encoding for transfer..."
base64 /tmp/production-data.tar.gz > /tmp/production-data-encoded.txt
echo "  ✓ Encoded to /tmp/production-data-encoded.txt"

echo ""
echo "🚀 Instructions for Heroku import:"
echo "=============================================="
echo ""
echo "1. Copy the following command and run it:"
echo ""
echo "heroku run bash"
echo ""
echo "2. In the Heroku shell, run these commands:"
echo ""
echo "# Create and decode the data"
echo "cat > /tmp/production-data-encoded.txt << 'EOF'"
cat /tmp/production-data-encoded.txt
echo "EOF"
echo ""
echo "base64 -d /tmp/production-data-encoded.txt > /tmp/production-data.tar.gz"
echo "tar -xzf /tmp/production-data.tar.gz -C /tmp/"
echo "ls /tmp/data-import/"
echo ""
echo "# Run the import (add --dry-run first to test)"
echo "python manage.py import_production_data --merge-users --data-dir /tmp/data-import"
echo ""
echo "=============================================="
echo ""
echo "🧹 Cleaning up local temp files..."
rm -f /tmp/production-data.tar.gz /tmp/production-data-encoded.txt
echo "  ✓ Cleaned up"

echo ""
echo "✅ Transfer preparation complete!"
echo ""
echo "💡 Pro tip: Run with --dry-run first to see what would happen"
