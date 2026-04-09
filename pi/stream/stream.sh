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
    OUTPUT_URL="srt://${SRT_HOST}:${SRT_PORT}?mode=${SRT_MODE}&latency=${SRT_LATENCY}"
    OUTPUT_URL="${OUTPUT_URL}&oheadbw=${SRT_OHEADBW:-25}"
    OUTPUT_URL="${OUTPUT_URL}&sndbuf=${SRT_SNDBUF:-1500000}&rcvbuf=${SRT_RCVBUF:-1500000}"
    if [ -n "${SRT_PASSPHRASE}" ]; then
        OUTPUT_URL="${OUTPUT_URL}&passphrase=${SRT_PASSPHRASE}"
    fi
    OUTPUT_FORMAT="mpegts"
    echo "=== KevinStream (SRT) ==="
    echo "Target: ${SRT_HOST}:${SRT_PORT} (latency: ${SRT_LATENCY}us, oheadbw: ${SRT_OHEADBW:-25}%)"
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
AUDIO_SYNC_ARGS=""
if [ "$AUDIO_DEVICE" != "none" ]; then
    AUDIO_ARGS="-use_wallclock_as_timestamps 1 -f alsa -ac 1 -ar 48000 -thread_queue_size 1024 -i ${AUDIO_DEVICE}"
    # aresample=async=1 continuously corrects A/V drift between independent USB devices
    # Uses default min_hard_comp=0.5s to avoid audible pops from aggressive compensation
    AUDIO_SYNC_ARGS="-c:a aac -ac 2 -ar 44100 -b:a ${AUDIO_BITRATE} -af aresample=async=1:first_pts=0"
fi

# Build encoder-specific args and rate control
ENCODER_ARGS=""
RATE_ARGS=""
if [ "${ENCODER}" = "h264_v4l2m2m" ]; then
    # Hardware encoder: uses its own internal rate controller, no maxrate/bufsize
    ENCODER_ARGS="-num_output_buffers ${NUM_OUTPUT_BUFFERS} -num_capture_buffers ${NUM_CAPTURE_BUFFERS}"
elif [ "${ENCODER}" = "libx264" ]; then
    # Software encoder: VBV rate control to cap bitrate spikes
    ENCODER_ARGS="-preset ${X264_PRESET:-ultrafast} -tune ${X264_TUNE:-zerolatency}"
    if [ -n "${MAXRATE:-}" ]; then
        RATE_ARGS="-maxrate ${MAXRATE} -bufsize ${BUFSIZE:-5000k}"
    fi
fi

exec ffmpeg \
    -use_wallclock_as_timestamps 1 \
    -f v4l2 \
    -thread_queue_size 1024 \
    -input_format "${VIDEO_INPUT_FORMAT}" \
    -video_size "${WIDTH}x${HEIGHT}" \
    -framerate "${FPS}" \
    -i "${VIDEO_DEVICE}" \
    ${AUDIO_ARGS} \
    -c:v "${ENCODER}" \
    -b:v "${BITRATE}" \
    ${RATE_ARGS} \
    -g "${GOP_SIZE}" \
    -pix_fmt "${PIX_FMT}" \
    ${ENCODER_ARGS} \
    ${AUDIO_SYNC_ARGS} \
    -max_muxing_queue_size 1024 \
    -stats_period 1 \
    -f "${OUTPUT_FORMAT}" \
    "${OUTPUT_URL}"
