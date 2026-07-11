#!/usr/bin/env python3
"""Publish the Meso walkthrough demo video (issue #388) for public hosting.

`just record-demo` (``scripts/record_demo.py``) writes a fresh
``docs/demo/out/meso-walkthrough.mp4`` — a git-ignored local artifact. This
script is the other half: it uploads that file to the ``masterfit`` S3 bucket
(where all other site media already lives) at a fixed, public key, plus a
poster frame for the landing page's ``<video poster=...>``, so the Meso
landing page (issue #415 follow-up) has something stable to embed.

The whole refresh loop, end to end, is two commands:

    just record-demo && just publish-demo-video

Both objects are uploaded to the *same* key every time (no versioning by
timestamp/hash) — the landing page's URL never changes, so re-running this
after a UI change is the entire "update the video" story; nothing in
settings or the template needs touching.

Publishing alone no longer puts the video *on* the page, though: issue #454
turned the landing embed off by default (the recording was confusing and
repeated the page's other visuals; the live sandbox at ``/meso/demo/`` is the
walkthrough now), so ``MESO_DEMO_VIDEO_URL`` defaults to ``""``. To put a new,
better recording back on the page, publish it with this script and *then* set
``MESO_DEMO_VIDEO_URL`` (and optionally ``MESO_DEMO_VIDEO_POSTER_URL``) in the
environment to the keys below.

Uploaded objects (``ACL: public-read``, matching every other object this app
puts in the bucket — see ``AWS_S3_OBJECT_PARAMETERS`` in
``config/settings/production.py``):

    s3://masterfit/meso/demo/meso-walkthrough.mp4
    s3://masterfit/meso/demo/meso-walkthrough-poster.webp

Run via ``just publish-demo-video`` (``uv run python
scripts/publish_demo_video.py``). Needs ``AWS_ACCESS_KEY_ID`` /
``AWS_SECRET_ACCESS_KEY`` for an IAM identity with ``s3:PutObject`` on the
bucket (see ``resolve_aws_credentials`` below for where those are read from)
and, for the poster, ``ffmpeg`` on ``PATH`` (or Playwright's bundled copy —
reuses ``record_demo.find_ffmpeg``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path

import boto3
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError
from dotenv import dotenv_values
from PIL import Image
from record_demo import REPO_ROOT
from record_demo import find_ffmpeg
from record_demo import log

VIDEO_PATH = REPO_ROOT / "docs" / "demo" / "out" / "meso-walkthrough.mp4"

BUCKET = "masterfit"
VIDEO_KEY = "meso/demo/meso-walkthrough.mp4"
POSTER_KEY = "meso/demo/meso-walkthrough-poster.webp"

# masterfit's actual bucket region (``aws s3api get-bucket-location --bucket
# masterfit`` -> us-west-1) — passed explicitly so boto3 talks to the right
# regional endpoint on the first request instead of round-tripping through
# botocore's cross-region redirect handling. Overridable for parity with how
# every other AWS_* setting in this app is env-overridable.
DEFAULT_AWS_REGION = "us-west-1"

# A real ~20s 1280x720 recording (docs/demo/README.md's storyboard) lands
# around 500-700 KB once ffmpeg's re-encoded it to h264/mp4. 50 KB is well
# below any legitimate recording and well above a truncated/zero-byte write —
# generous on purpose, this is a truncation/corruption guard, not a quality gate.
MIN_VIDEO_BYTES = 50 * 1024

# The poster is grabbed mid-video, not frame 0 (a blank/loading first paint).
# Timestamp picked by eyeballing a `ffmpeg -vf fps=1/2` contact sheet of the
# current recording: 8s lands on STEP 4's resolved agent proposal — the diff
# list ("Bulgarian Split Squat -> Hip Thrust", etc.) and "Review N changes" CTA
# next to the populated week grid, the single frame that best shows the
# propose -> review -> approve pitch. Re-eyeball this constant if a storyboard
# edit (record_demo.py) reshuffles step order/timing enough to land it
# somewhere less representative (e.g. a loading state or blank composer).
POSTER_TIMESTAMP_SECONDS = 8.0
POSTER_SIZE = (1280, 720)  # matches record_demo.VIEWPORT — no letterboxing
WEBP_QUALITY = 82
POSTER_MAX_BYTES = 60 * 1024  # comfortably under the "well under 100KB" bar


def check_video():
    if not VIDEO_PATH.exists():
        sys.exit(
            f"FAILED: {VIDEO_PATH.relative_to(REPO_ROOT)} doesn't exist — run "
            "`just record-demo` first."
        )
    # A successful record-demo run deletes its intermediate .webm after mp4
    # conversion — one left behind means the LATEST run fell back to WebM
    # (e.g. ffmpeg without libx264) and this .mp4 is a stale earlier take.
    fallback_webm = VIDEO_PATH.with_suffix(".webm")
    if fallback_webm.exists():
        sys.exit(
            f"FAILED: {fallback_webm.relative_to(REPO_ROOT)} exists, so the "
            "last `just record-demo` run fell back to WebM and the .mp4 here "
            "is from an older take. Fix the mp4 conversion (ffmpeg with "
            "libx264), re-run `just record-demo`, then publish."
        )
    size = VIDEO_PATH.stat().st_size
    if size < MIN_VIDEO_BYTES:
        sys.exit(
            f"FAILED: {VIDEO_PATH.relative_to(REPO_ROOT)} is only {size} bytes "
            f"(< {MIN_VIDEO_BYTES} byte floor) — looks empty/truncated, not a "
            "real recording. Re-run `just record-demo` and check its output."
        )
    return size


def extract_poster(video_path: Path) -> bytes:
    """Grab one frame at POSTER_TIMESTAMP_SECONDS and encode it as WebP.

    Mirrors ``capture_landing_still.optimize()``'s quality-stepping loop —
    same target (a small, well-compressed still), same tool (Pillow).
    """
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        sys.exit(
            "FAILED: no ffmpeg found (system PATH or Playwright's bundled "
            "copy) — can't extract a poster frame. Install ffmpeg and re-run."
        )
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_png = Path(tmpdir) / "poster-raw.png"
        cmd = [
            ffmpeg_path,
            "-y",
            "-ss",
            str(POSTER_TIMESTAMP_SECONDS),
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(raw_png),
        ]
        log(f"$ {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not raw_png.exists():
            sys.stderr.write(result.stderr)
            sys.exit("FAILED: ffmpeg couldn't extract the poster frame.")

        with Image.open(raw_png) as im:
            im = im.convert("RGB")
            if im.size != POSTER_SIZE:
                im = im.resize(POSTER_SIZE, Image.LANCZOS)
            quality = WEBP_QUALITY
            while True:
                buf = BytesIO()
                im.save(buf, "WEBP", quality=quality, method=6)
                data = buf.getvalue()
                if len(data) <= POSTER_MAX_BYTES or quality <= 40:
                    break
                quality -= 10
    return data


def resolve_aws_credentials():
    """Resolve the AWS creds this upload should authenticate with.

    Already-exported process env wins (CI/the Hetzner box, where a real
    deploy session may already have them), else ``.env`` — the same source
    ``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` come from everywhere else
    in this app (``get_env_var()`` in ``config/settings/base.py``).
    Deliberately doesn't fall through to boto3's own default chain (an IAM
    role, ``~/.aws/credentials``, ...) — that would let a missing/typo'd
    ``.env`` entry silently pick up some unrelated ambient credential instead
    of failing loudly.
    """
    from_env = {
        "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID"),
        "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY"),
        # Temporary STS/SSO credentials are only valid with their session
        # token; long-lived IAM user keys have none, so this stays optional.
        "aws_session_token": os.environ.get("AWS_SESSION_TOKEN"),
    }
    if from_env["aws_access_key_id"] and from_env["aws_secret_access_key"]:
        return from_env

    dotenv = dotenv_values(REPO_ROOT / ".env")
    from_dotenv = {
        "aws_access_key_id": dotenv.get("AWS_ACCESS_KEY_ID"),
        "aws_secret_access_key": dotenv.get("AWS_SECRET_ACCESS_KEY"),
        "aws_session_token": dotenv.get("AWS_SESSION_TOKEN"),
    }
    if from_dotenv["aws_access_key_id"] and from_dotenv["aws_secret_access_key"]:
        return from_dotenv

    sys.exit(
        "FAILED: no AWS credentials found (checked the process env, then "
        ".env) — set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY."
    )


def s3_client():
    creds = resolve_aws_credentials()
    region = os.environ.get("AWS_S3_REGION_NAME") or DEFAULT_AWS_REGION
    return boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=creds["aws_access_key_id"],
        aws_secret_access_key=creds["aws_secret_access_key"],
        aws_session_token=creds["aws_session_token"],
    )


def upload(s3, path_or_bytes, key, content_type, *, is_path):
    extra_args = {
        "ACL": "public-read",
        "ContentType": content_type,
        # These keys are fixed and overwritten on every publish — "no-cache"
        # makes browsers revalidate (ETag 304 when unchanged) instead of
        # serving a stale walkthrough for up to a day after a refresh.
        "CacheControl": "no-cache",
    }
    try:
        if is_path:
            # upload_file's transfer manager wraps any ClientError (e.g. a bad
            # key) in its own S3UploadFailedError rather than raising it
            # directly — catch both.
            s3.upload_file(
                Filename=str(path_or_bytes),
                Bucket=BUCKET,
                Key=key,
                ExtraArgs=extra_args,
            )
        else:
            s3.put_object(
                Bucket=BUCKET,
                Key=key,
                Body=path_or_bytes,
                **extra_args,
            )
    except (ClientError, S3UploadFailedError) as exc:
        sys.exit(
            f"FAILED: S3 upload of {key} failed ({exc}). If this is "
            "InvalidAccessKeyId/InvalidClientTokenId/SignatureDoesNotMatch, "
            "check AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in .env — that "
            "key may be stale/rotated."
        )


def public_url(key):
    return f"https://{BUCKET}.s3.amazonaws.com/{key}"


def main():
    video_size = check_video()
    log(f"video: {VIDEO_PATH.relative_to(REPO_ROOT)} ({video_size / 1024:.0f} KB)")

    poster_bytes = extract_poster(VIDEO_PATH)
    log(f"poster: extracted ({len(poster_bytes) / 1024:.1f} KB)")

    s3 = s3_client()

    log(f"uploading video -> s3://{BUCKET}/{VIDEO_KEY}")
    upload(s3, VIDEO_PATH, VIDEO_KEY, "video/mp4", is_path=True)

    log(f"uploading poster -> s3://{BUCKET}/{POSTER_KEY}")
    upload(s3, poster_bytes, POSTER_KEY, "image/webp", is_path=False)

    log(
        "\ndone:\n"
        f"  {public_url(VIDEO_KEY)} ({video_size / 1024:.0f} KB)\n"
        f"  {public_url(POSTER_KEY)} ({len(poster_bytes) / 1024:.1f} KB)"
    )


if __name__ == "__main__":
    main()
