# Eversense Zero
Eversense zero is a python based app that runs on a raspberry pi zero with a Display HAT to display the users blood sugar levers.

To build the app you need the following dependencies:
```bash
  python3 -m venv .venv
  source .venv/bin/activate
  pip install poetry
  poetry install --with dev
```

You will then be able to run the application on the PI Zero by running:
```bash
  python3 main.py
```