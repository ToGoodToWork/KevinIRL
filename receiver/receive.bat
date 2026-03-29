@echo off
REM KevinStream - SRT Receiver (Windows)
REM Receives the SRT stream from the Pi and forwards to OBS via UDP.
REM
REM Usage: receive.bat [PORT] [OBS_UDP_PORT]
REM
REM OBS Media Source: udp://127.0.0.1:9001
REM Or use SRT directly in OBS: srt://:9000?mode=listener

set SRT_PORT=%1
if "%SRT_PORT%"=="" set SRT_PORT=9000

set OBS_PORT=%2
if "%OBS_PORT%"=="" set OBS_PORT=9001

echo ================================================
echo          KevinStream SRT Receiver
echo ================================================
echo   SRT listening on port: %SRT_PORT%
echo   Forwarding to UDP: 127.0.0.1:%OBS_PORT%
echo.
echo   OBS Media Source: udp://127.0.0.1:%OBS_PORT%
echo   (or srt://:%SRT_PORT%?mode=listener)
echo.
echo   Press Ctrl+C to stop
echo ================================================
echo.

:loop
echo [%TIME%] Waiting for SRT connection on port %SRT_PORT%...

ffmpeg ^
    -hide_banner ^
    -loglevel warning ^
    -i "srt://0.0.0.0:%SRT_PORT%?mode=listener&latency=800000" ^
    -c:v copy ^
    -c:a aac -ac 2 -ar 44100 -b:a 96k ^
    -af "aresample=async=1000:first_pts=0" ^
    -max_interleave_delta 500000 ^
    -fflags +genpts+discardcorrupt ^
    -flags +low_delay ^
    -f mpegts ^
    "udp://127.0.0.1:%OBS_PORT%?pkt_size=1316"

echo.
echo [%TIME%] Stream ended. Reconnecting in 2s...
timeout /t 2 /nobreak >nul
goto loop
