#!/bin/bash
# KevinStream - FFmpeg streaming script
# Supports both RTMP and SRT output
# Uses Pi 4 hardware encoder (h264_v4l2m2m)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_FILE="${SCRIPT_DIR}/stream.conf"
CONF_EXAMPLE="${SCRIPT_DIR}/stream.conf.example"

# Bootstrap from the template on first run so a fresh clone or post-pull
# state always has a working stream.conf. The real file is gitignored.
if [ ! -f "$CONF_FILE" ] && [ -f "$CONF_EXAMPLE" ]; then
    cp "$CONF_EXAMPLE" "$CONF_FILE"
    echo "Created ${CONF_FILE} from template"
fi

if [ ! -f "$CONF_FILE" ]; then
    echo "ERROR: Config file not found: $CONF_FILE (no template at $CONF_EXAMPLE either)"
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
    # SRT requires passphrase length 10–79 chars. Outside that range the option
    # is rejected with "Operation not supported: Bad parameters" and ffmpeg
    # crashes. Degrade to unencrypted with a clear warning rather than crash.
    if [ -n "${SRT_PASSPHRASE}" ]; then
        pp_len=${#SRT_PASSPHRASE}
        if [ "$pp_len" -ge 10 ] && [ "$pp_len" -le 79 ]; then
            OUTPUT_URL="${OUTPUT_URL}&passphrase=${SRT_PASSPHRASE}"
        else
            echo "WARNING: SRT_PASSPHRASE is ${pp_len} chars — SRT requires 10–79." >&2
            echo "WARNING: Streaming UNENCRYPTED. Set a 10+ char passphrase to enable encryption." >&2
        fi
    fi
    OUTPUT_FORMAT="mpegts"
    echo "=== KevinStream (SRT) ==="
    echo "Target: ${SRT_HOST}:${SRT_PORT} (latency: ${SRT_LATENCY}us, oheadbw: ${SRT_OHEADBW:-25}%)"
else
    echo "ERROR: Unknown protocol '${PROTOCOL}'. Use 'rtmp' or 'srt'."
    exit 1
fi

# Safety defaults — refuse to pass empty values to ffmpeg (which would crash with
# "Error setting option b to value ." or similar). update_config() already validates,
# so this is a belt-and-suspenders fallback for hand-edited or migrated configs.
: "${BITRATE:=2500k}"
: "${WIDTH:=1280}"
: "${HEIGHT:=720}"
: "${FPS:=30}"
: "${GOP_SIZE:=${FPS}}"
: "${ENCODER:=libx264}"
: "${PIX_FMT:=yuv420p}"
: "${VIDEO_INPUT_FORMAT:=mjpeg}"
: "${VIDEO_DEVICE:=/dev/video0}"
: "${AUDIO_BITRATE:=96k}"
: "${AUDIO_CHANNELS:=2}"
: "${NUM_OUTPUT_BUFFERS:=16}"
: "${NUM_CAPTURE_BUFFERS:=8}"

echo "Video: ${WIDTH}x${HEIGHT}@${FPS}fps ${BITRATE}"
echo "Encoder: ${ENCODER}"
echo "Audio: ${AUDIO_DEVICE}"
echo "==================="

# Build audio arguments
AUDIO_ARGS=""
AUDIO_SYNC_ARGS=""
if [ "$AUDIO_DEVICE" != "none" ]; then
    AUDIO_ARGS="-use_wallclock_as_timestamps 1 -f alsa -ac ${AUDIO_CHANNELS:-2} -ar 48000 -thread_queue_size 1024 -i ${AUDIO_DEVICE}"
    # Keep native 48000Hz to avoid resampling overhead, async=1 corrects A/V drift
    AUDIO_SYNC_ARGS="-c:a aac -ac 2 -ar 48000 -b:a ${AUDIO_BITRATE} -af aresample=async=1"
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
