@echo off
rem Serve Florence-2-base for the bridge's vision feature (port 11435).
rem Uses .venv-florence (Python 3.8 + the PyTorch already installed there).
rem Extra options pass through, e.g. "tools\start_vision.bat --cpu" to keep it off the GPU.

"%~dp0..\.venv-florence\Scripts\python.exe" "%~dp0florence_server.py" --model "%~dp0..\models\Florence-2-base" %*
