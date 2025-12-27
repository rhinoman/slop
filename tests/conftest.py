"""
Pytest fixtures for SLOP test suite
"""

import pytest
import sys
from pathlib import Path

# Add src to path for development
src_path = Path(__file__).parent.parent / "src"
if src_path.exists():
    sys.path.insert(0, str(src_path))


@pytest.fixture
def examples_dir():
    """Path to examples directory"""
    return Path(__file__).parent.parent / "examples"


@pytest.fixture
def rate_limiter_source(examples_dir):
    """Rate limiter example source"""
    return (examples_dir / "rate-limiter.slop").read_text()


@pytest.fixture
def hello_source(examples_dir):
    """Hello world example source"""
    return (examples_dir / "hello.slop").read_text()


@pytest.fixture
def comprehensive_source():
    """Comprehensive transpiler test source"""
    return (Path(__file__).parent / "comprehensive.slop").read_text()
