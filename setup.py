"""
Minimal setup.py for backward compatibility.
Configuration is in pyproject.toml.
"""
from setuptools import setup, find_packages

# All configuration is in pyproject.toml
setup(
    name="cityworks",
    version="0.1.0",
    packages=find_packages(),
)