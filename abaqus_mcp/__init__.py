"""Abaqus MCP / agent engine.

Runs on the *system* Python 3 interpreter. Anything that must run inside the
Abaqus kernel (Python 2.7 for Abaqus 2022) lives under ``scripts_py27`` and is
invoked as a subprocess -- never imported here.
"""

__version__ = "0.2.0"
