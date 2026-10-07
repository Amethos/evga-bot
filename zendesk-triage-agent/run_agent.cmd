@echo off
rem Starts the agent in the background. Used by the scheduled task (see README).
cd /d "%~dp0"
if not exist data mkdir data
".venv\Scripts\pythonw.exe" -m triage_agent run >> data\agent.log 2>&1
