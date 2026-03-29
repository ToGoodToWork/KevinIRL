#!/bin/bash
# KevinStream - FFmpeg SRT streaming script
# Uses Pi 4 hardware encoder (h264_v4l2m2m)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_FILE="${SCRIPT_DIR}/stream.conf"

if [ ! -f "$CONF_FILE" ]; then
    echo "ERROR: Config file not found: $CONF_FILE"
    exit 1
fi

# Load configuration
source "$CONF_FILE"

# Build SRT URL
SRT_URL="srt://${SRT_HOST}:${SRT_PORT}?mode=${SRT_MODE}&latency=${SRT_LATENCY}&passphrase=${SRT_PASSPHRASE}"

# Build audio arguments
AUDIO_ARGS=""
if [ "$AUDIO_DEVICE" != "none" ]; then
    AUDIO_ARGS="-f alsa -i ${AUDIO_DEVICE} -c:a aac -b:a ${AUDIO_BITRATE}"
fi

echo "=== KevinStream ==="
echo "Video: ${WIDTH}x${HEIGHT}@${FPS}fps ${BITRATE}"
echo "Encoder: ${ENCODER}"
echo "SRT target: ${SRT_HOST}:${SRT_PORT} (latency: ${SRT_LATENCY}us)"
echo "Audio: ${AUDIO_DEVICE}"
echo "==================="

exec ffmpeg \
    -f v4l2 \
    -input_format "${VIDEO_INPUT_FORMAT}" \
    -video_size "${WIDTH}x${HEIGHT}" \
    -framerate "${FPS}" \
    -i "${VIDEO_DEVICE}" \
    ${AUDIO_ARGS} \
    -c:v "${ENCODER}" \
    -b:v "${BITRATE}" \
    -g "${GOP_SIZE}" \
    -pix_fmt "${PIX_FMT}" \
    -num_output_buffers "${NUM_OUTPUT_BUFFERS}" \
    -num_capture_buffers "${NUM_CAPTURE_BUFFERS}" \
    -f mpegts \
    "${SRT_URL}"
