@echo off
rem Starts lan-streamer and opens the player page in the browser.
rem Options: --fps 30|60  --encoder auto|nvidia|amd|intel|cpu  --source <file or URL>  --switch-interval <seconds>
title lan-streamer
"%~dp0python\python.exe" -m lanstreamer %*
if errorlevel 1 pause
