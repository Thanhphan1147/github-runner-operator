# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for github runner charm."""


import argparse


def pytest_addoption(parser: argparse.ArgumentParser):
    """Add options to pytest parser."""
    parser.addoption("--path", action="store")
    parser.addoption("--token", action="store")