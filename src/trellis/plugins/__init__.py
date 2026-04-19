"""Plugin discovery via Python entry points.

Client packages extend Trellis in one of two ways:

1. **Client-side extractors** — pure data submission via
   ``POST /api/v1/extract/drafts``.  Ships in your own package with
   your own dependencies; no server-side code change.  See
   :mod:`trellis_sdk.extract` and Playbook 13.

2. **Runtime extensions (this module)** — code that has to run
   inside the Trellis API process.  Custom store backends, custom
   LLM providers, classifiers, rerankers, policy gates, search
   strategies.  Ships as a wheel installed alongside ``trellis-api``
   and registered via Python entry points.

Plugin authors declare entry points in their ``pyproject.toml``::

    [project.entry-points."trellis.stores.graph"]
    unity_native = "trellis_unity_catalog.stores:UCGraphStore"

    [project.entry-points."trellis.llm.providers"]
    bedrock = "trellis_llm_bedrock:BedrockClient"

Trellis config selects the plugin by its entry-point name::

    # ~/.config/trellis/config.yaml
    stores:
      graph:
        backend: unity_native
    llm:
      provider: bedrock

**Shadowing policy.**  Built-ins win when a plugin uses the same
name (``sqlite``, ``postgres``, ``openai``, ``anthropic``).
Plugins that shadow a built-in log a warning at discovery time.
Operators who *do* want to override a built-in set
``TRELLIS_PLUGIN_OVERRIDE=1`` in the environment — explicit
override, no silent takeover.

**Group names.**  All supported groups are constants in this
module — see :data:`GROUP_STORES`, :data:`GROUP_LLM_PROVIDERS`,
etc.  Adding a new group means adding a constant here, wiring
the consumer registry, and documenting it in the ADR at
``docs/design/adr-plugin-contract.md``.

See also: :mod:`trellis.plugins.loader` for the ``discover()``
helper, and :mod:`trellis.plugins.diagnostic` for the data
structure that backs ``trellis admin check-plugins``.
"""

from trellis.plugins.diagnostic import (
    PluginEntry,
    PluginReport,
    collect_plugin_report,
)
from trellis.plugins.loader import (
    GROUP_CLASSIFIERS,
    GROUP_EXTRACTORS,
    GROUP_LLM_EMBEDDERS,
    GROUP_LLM_PROVIDERS,
    GROUP_POLICIES,
    GROUP_RERANKERS,
    GROUP_SEARCH_STRATEGIES,
    GROUP_STORES,
    OVERRIDE_ENV,
    PluginSpec,
    discover,
    load_class,
    merge_with_builtins,
    store_backend_groups,
)

__all__ = [
    # Groups
    "GROUP_CLASSIFIERS",
    "GROUP_EXTRACTORS",
    "GROUP_LLM_EMBEDDERS",
    "GROUP_LLM_PROVIDERS",
    "GROUP_POLICIES",
    "GROUP_RERANKERS",
    "GROUP_SEARCH_STRATEGIES",
    "GROUP_STORES",
    # Env var
    "OVERRIDE_ENV",
    # Loader
    "PluginSpec",
    "discover",
    "load_class",
    "merge_with_builtins",
    "store_backend_groups",
    # Diagnostic
    "PluginEntry",
    "PluginReport",
    "collect_plugin_report",
]
