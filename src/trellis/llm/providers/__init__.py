"""Reference LLM provider implementations.

Providers are gated behind optional extras to keep core dependency-free:

- ``pip install trellis-ai[llm-openai]`` for ``OpenAIClient`` / ``OpenAIEmbedder``
- ``pip install trellis-ai[llm-anthropic]`` for ``AnthropicClient``

Import directly from the submodule (e.g. ``from trellis.llm.providers.openai
import OpenAIClient``) — the package ``__init__`` does not re-export them so
that a missing extra raises :class:`ModuleNotFoundError` at the import site.
"""
