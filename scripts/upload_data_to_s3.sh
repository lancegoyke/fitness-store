#!/bin/bash

# Script to upload data files to S3 for secure transfer
# Usage: ./scripts/upload_data_to_s3.sh <s3-bucket-path>
# Example: ./scripts/upload_data_to_s3.sh my-private-bucket/import-data

set -e

if [ $# -eq 0 ]; then
    echo "❌ Error: Please provide S3 bucket path"
    echo "Usage: $0 <s3-bucket-path>"
    echo "Example: $0 my-private-bucket/import-data"
    exit 1
fi

S3_PATH="$1"

echo "🔐 Uploading production data to S3..."

# Check if data files exist
if [ ! -d "data-import" ]; then
    echo "❌ Error: data-import directory not found!"
    exit 1
fi

# Check AWS CLI
if ! command -v aws &> /dev/null; then
    echo "❌ Error: AWS CLI not installed"
    echo "Install with: pip install awscli"
    exit 1
fi

# Check AWS credentials
if ! aws sts get-caller-identity &> /dev/null; then
    echo "❌ Error: AWS credentials not configured"
    echo "Run: aws configure"
    exit 1
fi

echo "📤 Uploading files to s3://$S3_PATH/"

# Upload each file
files=(
    "production-users.json"
    "production-tags.json"
    "production-challenges.json"
    "production-records.json"
    "production-tagged_items.json"
)

for file in "${files[@]}"; do
    if [ -f "data-import/$file" ]; then
        echo "  📤 Uploading $file..."
        aws s3 cp "data-import/$file" "s3://$S3_PATH/$file"
        echo "  ✓ $file uploaded"
    else
        echo "  ⚠️  $file not found, skipping"
    fi
done

echo ""
echo "✅ Upload complete!"
echo ""
echo "🚀 To import on Heroku, run:"
echo "heroku run python manage.py import_production_data --merge-users --s3-bucket '$S3_PATH'"
echo ""
echo "🧹 Don't forget to delete the files from S3 after import:"
echo "aws s3 rm s3://$S3_PATH/ --recursive"
