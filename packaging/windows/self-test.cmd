@echo off
rem Checks GStreamer, the required elements and the video encoders on this PC.
rem Optional: self-test.cmd --encoder nvidia|amd|intel|cpu
title lan-streamer self-test
"%~dp0python\python.exe" -m lanstreamer.doctor %*
echo.
pause
