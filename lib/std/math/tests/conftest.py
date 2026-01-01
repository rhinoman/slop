"""
Pytest fixtures for SLOP math standard library tests
"""

import shutil
import sys
from pathlib import Path

import pytest

# Add src to path for development
src_path = Path(__file__).parents[4] / "src"
if src_path.exists():
    sys.path.insert(0, str(src_path))


@pytest.fixture
def c_compiler():
    """Get the C compiler command, or skip if none available"""
    if shutil.which("cc"):
        return "cc"
    elif shutil.which("gcc"):
        return "gcc"
    else:
        pytest.skip("No C compiler available")


@pytest.fixture
def runtime_path():
    """Path to the SLOP runtime header"""
    return Path(__file__).parents[4] / "src" / "slop" / "runtime"


@pytest.fixture
def lib_path():
    """Path to the lib directory"""
    return Path(__file__).parents[3]


@pytest.fixture
def math_tests_path():
    """Path to the math tests directory"""
    return Path(__file__).parent
