"""
SLOP LLM Providers - Configurable LLM provider system

Supports:
- Ollama for local models
- OpenAI-compatible API (OpenAI, vLLM, LMStudio, Anthropic, etc.)
- TOML configuration with ${ENV_VAR} substitution
"""

import json
import os
import re
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional, Any
from enum import Enum


class Tier(Enum):
    TIER_1 = 1  # 1-3B models
    TIER_2 = 2  # 7-8B models
    TIER_3 = 3  # 13-34B models
    TIER_4 = 4  # 70B+ models


@dataclass
class ModelConfig:
    provider: str
    model: str
    context_length: int = 4096
    temperature: float = 0.4


class Provider(ABC):
    """Base provider interface"""

    @abstractmethod
    def complete(self, prompt: str, config: ModelConfig) -> str:
        """Generate completion for the given prompt"""
        pass


class OllamaProvider(Provider):
    """Local Ollama models via HTTP API"""

    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url.rstrip('/')

    def complete(self, prompt: str, config: ModelConfig) -> str:
        url = f"{self.base_url}/api/generate"
        payload = {
            "model": config.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": config.temperature,
                "num_ctx": config.context_length
            }
        }

        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=data,
            headers={'Content-Type': 'application/json'}
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode('utf-8'))
                return result.get('response', '')
        except urllib.error.URLError as e:
            raise RuntimeError(f"Ollama request failed: {e}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Ollama HTTP error {e.code}: {e.read().decode()}")


class OpenAICompatibleProvider(Provider):
    """OpenAI-compatible API (OpenAI, vLLM, LMStudio, Anthropic, etc.)"""

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key

    def complete(self, prompt: str, config: ModelConfig) -> str:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": config.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": config.temperature,
            "max_tokens": 2048
        }

        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'

        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode('utf-8'))
                choices = result.get('choices', [])
                if choices:
                    return choices[0].get('message', {}).get('content', '')
                return ''
        except urllib.error.URLError as e:
            raise RuntimeError(f"API request failed: {e}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"API HTTP error {e.code}: {e.read().decode()}")


class MockProvider(Provider):
    """Mock LLM provider for testing"""

    def complete(self, prompt: str, config: ModelConfig) -> str:
        # Return mock responses based on prompt content
        prompt_lower = prompt.lower()
        if "withdraw" in prompt_lower or "balance" in prompt_lower:
            return """(if (< (. account balance) amount)
  (error 'insufficient-funds)
  (ok (put account balance (- (. account balance) amount))))"""
        elif "hash" in prompt_lower:
            return "(crypto-hash-password (. input password))"
        elif "adult" in prompt_lower:
            return "(>= (. user age) 18)"
        elif "add" in prompt_lower and "x" in prompt_lower and "y" in prompt_lower:
            return "(+ x y)"
        elif "sum" in prompt_lower:
            return "(+ x y)"
        else:
            return "(ok nil)"


class InteractiveProvider(Provider):
    """Route holes to interactive tools like Claude Code, Cursor, or Aider.

    This allows users to leverage their existing AI tool subscriptions
    instead of making direct API calls.

    Modes:
        clipboard: Copy prompt to clipboard, wait for user to paste response
        file: Write prompt to file, wait for user to indicate response file ready
    """

    def __init__(self, command: str = "claude", mode: str = "clipboard"):
        self.command = command
        self.mode = mode

    def complete(self, prompt: str, config: ModelConfig) -> str:
        if self.mode == "clipboard":
            return self._clipboard_mode(prompt)
        elif self.mode == "file":
            return self._file_mode(prompt)
        else:
            raise ValueError(f"Unknown interactive mode: {self.mode}")

    def _clipboard_mode(self, prompt: str) -> str:
        """Copy prompt to clipboard, wait for user to paste response"""
        try:
            import pyperclip
            pyperclip.copy(prompt)
            print(f"\n→ Prompt copied to clipboard ({len(prompt)} chars)")
            print(f"  Paste into {self.command} and copy the response")
        except ImportError:
            # Fallback: write to temp file if pyperclip not available
            from pathlib import Path
            request_file = Path(".slop-hole-request")
            request_file.write_text(prompt)
            print(f"\n→ Prompt written to {request_file} ({len(prompt)} chars)")
            print(f"  Copy contents to {self.command}")

        print()
        response = input("← Paste response (or enter file path): ").strip()

        # Handle file path input
        if response.startswith("/") or response.startswith("./") or response.startswith("~"):
            from pathlib import Path
            path = Path(response).expanduser()
            if path.exists():
                return path.read_text()
            else:
                print(f"  Warning: File not found: {path}")
                return response

        return response

    def _file_mode(self, prompt: str) -> str:
        """Write prompt to file, wait for response file"""
        from pathlib import Path
        request_file = Path(".slop-hole-request")
        response_file = Path(".slop-hole-response")

        request_file.write_text(prompt)
        print(f"\n→ Prompt written to {request_file}")
        print(f"  Write response to {response_file}")

        input(f"← Press Enter when {response_file} is ready: ")

        if response_file.exists():
            return response_file.read_text()
        else:
            raise RuntimeError(f"Response file not found: {response_file}")

    def is_interactive(self) -> bool:
        """Returns True - this provider requires user interaction"""
        return True


# --- Configuration Loading ---

def expand_env_vars(value: str) -> str:
    """Expand ${VAR_NAME} patterns in strings"""
    pattern = r'\$\{([^}]+)\}'
    def replace(match):
        var = match.group(1)
        return os.environ.get(var, "")
    return re.sub(pattern, replace, value)


def expand_env_vars_recursive(obj: Any) -> Any:
    """Recursively expand env vars in all string values"""
    if isinstance(obj, str):
        return expand_env_vars(obj)
    elif isinstance(obj, dict):
        return {k: expand_env_vars_recursive(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [expand_env_vars_recursive(item) for item in obj]
    return obj


def load_config(path: str) -> dict:
    """Load TOML config with env var expansion"""
    try:
        import tomllib
    except ImportError:
        # Python < 3.11 fallback
        try:
            import tomli as tomllib
        except ImportError:
            raise RuntimeError(
                "TOML parsing requires Python 3.11+ or 'tomli' package. "
                "Install with: pip install tomli"
            )

    with open(path, 'rb') as f:
        config = tomllib.load(f)

    return expand_env_vars_recursive(config)


def create_provider(provider_config: dict) -> Provider:
    """Create a provider instance from config"""
    provider_type = provider_config.get('type', '')

    if provider_type == 'ollama':
        base_url = provider_config.get('base_url', 'http://localhost:11434')
        return OllamaProvider(base_url)

    elif provider_type == 'openai-compatible':
        base_url = provider_config.get('base_url', 'https://api.openai.com/v1')
        api_key = provider_config.get('api_key', '')
        return OpenAICompatibleProvider(base_url, api_key)

    elif provider_type == 'interactive':
        command = provider_config.get('command', 'claude')
        mode = provider_config.get('mode', 'clipboard')
        return InteractiveProvider(command, mode)

    elif provider_type == 'mock':
        return MockProvider()

    else:
        raise ValueError(f"Unknown provider type: {provider_type}")


class MultiProvider(Provider):
    """Routes requests to the appropriate provider based on tier config"""

    def __init__(self, providers: Dict[str, Provider]):
        self.providers = providers
        self._current_provider_name: Optional[str] = None

    def set_provider(self, name: str):
        """Set which provider to use for subsequent calls"""
        if name not in self.providers:
            raise ValueError(f"Unknown provider: {name}. Available: {list(self.providers.keys())}")
        self._current_provider_name = name

    def complete(self, prompt: str, config: ModelConfig) -> str:
        provider_name = config.provider
        if provider_name not in self.providers:
            raise ValueError(f"Provider '{provider_name}' not configured")
        return self.providers[provider_name].complete(prompt, config)

    def is_interactive(self, provider_name: str) -> bool:
        """Check if a provider is interactive (requires user input)"""
        if provider_name not in self.providers:
            return False
        provider = self.providers[provider_name]
        return hasattr(provider, 'is_interactive') and provider.is_interactive()


def create_from_config(config: dict) -> tuple[Dict[Tier, ModelConfig], Provider]:
    """
    Build tier configs and provider from TOML config.

    Returns:
        (tier_configs, multi_provider)
    """
    # Create provider instances
    providers_config = config.get('providers', {})
    providers: Dict[str, Provider] = {}

    for name, pconfig in providers_config.items():
        providers[name] = create_provider(pconfig)

    # If no providers configured, use mock
    if not providers:
        providers['mock'] = MockProvider()

    # Build tier configurations
    tiers_config = config.get('tiers', {})
    tier_configs: Dict[Tier, ModelConfig] = {}

    tier_map = {
        'tier-1': Tier.TIER_1,
        'tier-2': Tier.TIER_2,
        'tier-3': Tier.TIER_3,
        'tier-4': Tier.TIER_4,
    }

    for tier_name, tconfig in tiers_config.items():
        tier = tier_map.get(tier_name)
        if tier is None:
            continue

        tier_configs[tier] = ModelConfig(
            provider=tconfig.get('provider', 'mock'),
            model=tconfig.get('model', ''),
            context_length=tconfig.get('context_length', 4096),
            temperature=tconfig.get('temperature', 0.4)
        )

    # Apply routing config if present
    routing = config.get('routing', {})
    # Could store max_retries, escalate_on_failure for HoleFiller to use

    multi_provider = MultiProvider(providers)
    return tier_configs, multi_provider


def create_default_configs() -> Dict[Tier, ModelConfig]:
    """Default model configurations (used when no config file)"""
    return {
        Tier.TIER_1: ModelConfig('ollama', 'phi3:mini', 2048, 0.3),
        Tier.TIER_2: ModelConfig('ollama', 'llama3:8b', 4096, 0.4),
        Tier.TIER_3: ModelConfig('ollama', 'llama3:70b-q4', 8192, 0.5),
        Tier.TIER_4: ModelConfig('openai', 'gpt-4o', 128000, 0.6),
    }
