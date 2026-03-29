#!/bin/bash
# KevinStream - FFmpeg streaming script
# Supports both RTMP and SRT output
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

# Build output URL based on protocol
if [ "${PROTOCOL}" = "rtmp" ]; then
    OUTPUT_URL="${RTMP_URL}"
    OUTPUT_FORMAT="flv"
    echo "=== KevinStream (RTMP) ==="
    echo "Target: ${RTMP_URL}"
elif [ "${PROTOCOL}" = "srt" ]; then
    OUTPUT_URL="srt://${SRT_HOST}:${SRT_PORT}?mode=${SRT_MODE}&latency=${SRT_LATENCY}&passphrase=${SRT_PASSPHRASE}"
    OUTPUT_FORMAT="mpegts"
    echo "=== KevinStream (SRT) ==="
    echo "Target: ${SRT_HOST}:${SRT_PORT} (latency: ${SRT_LATENCY}us)"
else
    echo "ERROR: Unknown protocol '${PROTOCOL}'. Use 'rtmp' or 'srt'."
    exit 1
fi

echo "Video: ${WIDTH}x${HEIGHT}@${FPS}fps ${BITRATE}"
echo "Encoder: ${ENCODER}"
echo "Audio: ${AUDIO_DEVICE}"
echo "==================="

# Build audio arguments
AUDIO_ARGS=""
if [ "$AUDIO_DEVICE" != "none" ]; then
    AUDIO_ARGS="-f alsa -i ${AUDIO_DEVICE} -c:a aac -b:a ${AUDIO_BITRATE}"
fi

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
    -f "${OUTPUT_FORMAT}" \
    "${OUTPUT_URL}"
