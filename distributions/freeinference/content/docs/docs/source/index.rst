FreeInference Documentation
===================================

**Free LLM inference for coding agents and IDEs**

FreeInference provides free access to state-of-the-art language models
for coding agents and AI-powered IDEs, with a particularly smooth setup path for Kilo Code.

Quick Links
-----------

* :doc:`quickstart` - Get started in 5 minutes
* :doc:`integrations` - Configure with Kilo Code, Cursor, Roo Code, and other coding agents
* :doc:`claude-code` - Use Claude Code with FreeInference's Anthropic-compatible endpoint
* :doc:`agents` - Run coding agents on your GitHub repos in an isolated cloud sandbox
* :doc:`models` - View available models
* :doc:`api_headers` - API headers reference

.. toctree::
   :maxdepth: 2
   :caption: Getting Started:
   :hidden:

   quickstart
   integrations
   agents
   models
   api_headers

Key Features
------------

**Free Access**
   Free inference for coding agents and development tools

**Multiple Models**
   Access GLM, Qwen, MiniMax, and other powerful models

**IDE Integration**
   Easy setup with Kilo Code, Cursor, Roo Code, and more

**Cloud Agents (Beta)**
   Delegate a task, get back a draft pull request — agents run in isolated
   cloud sandboxes with your choice of agent and model

**Kilo-Friendly Setup**
   Detailed Kilo Code instructions for a fast OpenAI-compatible configuration



Getting Started
---------------

1. **Get your API key** - Register at `https://freeinference.org <https://freeinference.org>`_ and create your API key

2. **Choose your IDE:**

   - :doc:`Kilo Code <integrations>` - Kilo Code setup with recommended models
   - :doc:`Cursor <integrations>` - AI-powered code editor

3. **Configure and start coding!**

See the :doc:`quickstart` guide for detailed setup instructions.

Available Models
----------------

.. list-table::
   :header-rows: 1
   :widths: 28 10 16 46

   * - Model
     - Access
     - Context Length
     - Best For
   * - GLM-5.1
     - Free
     - 200K tokens
     - General coding, bilingual, thinking
   * - **DeepSeek V4 Flash** :sup:`agentic`
     - Free
     - 1M tokens
     - Complex coding and long reasoning chains
   * - **Qwen3.6 35B** :sup:`fastest`
     - Free
     - 262K tokens
     - Quick edits and background calls
   * - MiniMax M3
     - Free
     - 1M tokens
     - Long-context and multimodal work
   * - MiniMax M2.5
     - Free
     - 205K tokens
     - General reasoning with long output
   * - DiffusionGemma
     - Free
     - 262K tokens
     - Fast local text generation
   * - **GLM-5.3** :sup:`strongest coding`
     - Pro
     - 1M tokens
     - Strongest coding results; always reasons
   * - GLM-5.2
     - Pro
     - 1M tokens
     - Long context with switchable thinking
   * - Kimi K2.7 Code
     - Pro
     - 262K tokens
     - Coding-agent workflows

Models marked **Pro** need a Pro-enabled key; the rest are available to every
account. ``bge-m3`` is also available for codebase indexing via
``/v1/embeddings``.

See the complete :doc:`models` list for all available models.

Support
-------

Need help? Check out:

* :doc:`integrations` - IDE setup guides
* :doc:`models` - Available models
* GitHub Issues - Report bugs or request features
