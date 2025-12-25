"""
Tests for SLOP providers including InteractiveProvider
"""

import pytest
from unittest.mock import patch, MagicMock
from slop.providers import (
    Provider, MockProvider, OllamaProvider, OpenAICompatibleProvider,
    InteractiveProvider, MultiProvider, ModelConfig, Tier,
    create_provider, create_from_config, expand_env_vars
)


class TestMockProvider:
    """Test mock provider responses"""

    def test_withdraw_response(self):
        provider = MockProvider()
        config = ModelConfig('mock', 'mock')
        response = provider.complete("Check if balance sufficient for withdraw", config)
        assert "balance" in response.lower() or "insufficient" in response.lower()

    def test_adult_check_response(self):
        provider = MockProvider()
        config = ModelConfig('mock', 'mock')
        response = provider.complete("Check if user is adult", config)
        assert "age" in response.lower() or "18" in response


class TestInteractiveProvider:
    """Test interactive provider functionality"""

    def test_init_defaults(self):
        provider = InteractiveProvider()
        assert provider.command == "claude"
        assert provider.mode == "clipboard"

    def test_init_custom(self):
        provider = InteractiveProvider(command="cursor", mode="file")
        assert provider.command == "cursor"
        assert provider.mode == "file"

    def test_is_interactive(self):
        provider = InteractiveProvider()
        assert provider.is_interactive() is True

    def test_invalid_mode_raises(self):
        provider = InteractiveProvider(mode="invalid")
        config = ModelConfig('interactive', 'interactive')
        with pytest.raises(ValueError, match="Unknown interactive mode"):
            provider.complete("test prompt", config)

    @patch('builtins.input', return_value="(+ 1 2)")
    def test_clipboard_mode_direct_response(self, mock_input):
        """Test clipboard mode with direct text response"""
        provider = InteractiveProvider(mode="clipboard")
        config = ModelConfig('interactive', 'interactive')

        # Mock pyperclip not available - falls back to file
        with patch.dict('sys.modules', {'pyperclip': None}):
            with patch('pathlib.Path.write_text'):
                response = provider.complete("test prompt", config)
                assert response == "(+ 1 2)"

    @patch('builtins.input', return_value="./response.txt")
    @patch('pathlib.Path.exists', return_value=True)
    @patch('pathlib.Path.read_text', return_value="(ok result)")
    def test_clipboard_mode_file_path(self, mock_read, mock_exists, mock_input):
        """Test clipboard mode with file path response"""
        provider = InteractiveProvider(mode="clipboard")
        config = ModelConfig('interactive', 'interactive')

        with patch.dict('sys.modules', {'pyperclip': None}):
            with patch('pathlib.Path.write_text'):
                response = provider.complete("test prompt", config)
                assert response == "(ok result)"

    @patch('builtins.input', return_value="")
    @patch('pathlib.Path.exists', return_value=True)
    @patch('pathlib.Path.read_text', return_value="(error result)")
    @patch('pathlib.Path.write_text')
    def test_file_mode(self, mock_write, mock_read, mock_exists, mock_input):
        """Test file-based handoff mode"""
        provider = InteractiveProvider(mode="file")
        config = ModelConfig('interactive', 'interactive')

        response = provider.complete("test prompt", config)
        assert response == "(error result)"
        mock_write.assert_called_once()


class TestMultiProvider:
    """Test multi-provider routing"""

    def test_routes_to_correct_provider(self):
        mock1 = MockProvider()
        mock2 = MockProvider()
        multi = MultiProvider({'mock1': mock1, 'mock2': mock2})

        config = ModelConfig('mock1', 'mock')
        # Should route to mock1
        response = multi.complete("adult check", config)
        assert response

    def test_unknown_provider_raises(self):
        multi = MultiProvider({'mock': MockProvider()})
        config = ModelConfig('unknown', 'mock')
        with pytest.raises(ValueError, match="not configured"):
            multi.complete("test", config)

    def test_is_interactive_check(self):
        mock = MockProvider()
        interactive = InteractiveProvider()
        multi = MultiProvider({'mock': mock, 'interactive': interactive})

        assert multi.is_interactive('mock') is False
        assert multi.is_interactive('interactive') is True
        assert multi.is_interactive('unknown') is False


class TestCreateProvider:
    """Test provider creation from config"""

    def test_create_ollama_provider(self):
        config = {'type': 'ollama', 'base_url': 'http://localhost:11434'}
        provider = create_provider(config)
        assert isinstance(provider, OllamaProvider)
        assert provider.base_url == 'http://localhost:11434'

    def test_create_openai_compatible_provider(self):
        config = {
            'type': 'openai-compatible',
            'base_url': 'https://api.openai.com/v1',
            'api_key': 'test-key'
        }
        provider = create_provider(config)
        assert isinstance(provider, OpenAICompatibleProvider)
        assert provider.api_key == 'test-key'

    def test_create_interactive_provider(self):
        config = {
            'type': 'interactive',
            'command': 'cursor',
            'mode': 'file'
        }
        provider = create_provider(config)
        assert isinstance(provider, InteractiveProvider)
        assert provider.command == 'cursor'
        assert provider.mode == 'file'

    def test_create_mock_provider(self):
        config = {'type': 'mock'}
        provider = create_provider(config)
        assert isinstance(provider, MockProvider)

    def test_unknown_type_raises(self):
        config = {'type': 'unknown'}
        with pytest.raises(ValueError, match="Unknown provider type"):
            create_provider(config)


class TestEnvVarExpansion:
    """Test environment variable expansion"""

    def test_expand_single_var(self):
        with patch.dict('os.environ', {'TEST_VAR': 'test_value'}):
            result = expand_env_vars("prefix_${TEST_VAR}_suffix")
            assert result == "prefix_test_value_suffix"

    def test_expand_missing_var(self):
        result = expand_env_vars("${MISSING_VAR}")
        assert result == ""

    def test_no_vars_unchanged(self):
        result = expand_env_vars("no variables here")
        assert result == "no variables here"


class TestCreateFromConfig:
    """Test full config loading"""

    def test_creates_tier_configs(self):
        config = {
            'providers': {
                'mock': {'type': 'mock'}
            },
            'tiers': {
                'tier-1': {'provider': 'mock', 'model': 'test', 'temperature': 0.3},
                'tier-2': {'provider': 'mock', 'model': 'test2'}
            }
        }
        tier_configs, provider = create_from_config(config)

        assert Tier.TIER_1 in tier_configs
        assert Tier.TIER_2 in tier_configs
        assert tier_configs[Tier.TIER_1].temperature == 0.3

    def test_defaults_to_mock_provider(self):
        config = {'tiers': {}}
        tier_configs, provider = create_from_config(config)
        assert isinstance(provider, MultiProvider)
