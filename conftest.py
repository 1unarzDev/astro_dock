# Quarantine opt-in: the workspace agent tools' pytest plugin runs quarantined tests as xfail; a no-op
# when that package is not importable (its parent, <workspace>/src, is not on PYTHONPATH).
pytest_plugins = ['mhseals_agents.pytest_quarantine'] if __import__('importlib.util').util.find_spec('mhseals_agents') else []
