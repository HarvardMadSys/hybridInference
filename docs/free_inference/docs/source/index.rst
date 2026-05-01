FreeInference Documentation
===================================

**Free LLM inference for coding agents and IDEs**

FreeInference provides free access to state-of-the-art language models
specifically designed for coding agents like Cursor, Codex, Roo Code, and other AI-powered IDEs.

Quick Links
-----------

* :doc:`quickstart` - Get started in 5 minutes
* :doc:`integrations` - Configure with Cursor, Codex, and other coding agents
* :doc:`models` - View available models

.. toctree::
   :maxdepth: 2
   :caption: Getting Started:
   :hidden:

   quickstart
   integrations
   models

Key Features
------------

**Free Access**
   Free inference for coding agents and development tools

**Multiple Models**
   Access GLM, Qwen, MiniMax, Llama, and other powerful models

**Codebase Indexing**
   Free embedding endpoint (BGE-M3) for semantic code search in Roo Code and Kilo Code

**IDE Integration**
   Easy setup with Cursor, Codex, Roo Code, Kilo Code, and more



Getting Started
---------------

1. **Get your API key** - Register at `https://freeinference.org <https://freeinference.org>`_ and create your API key

2. **Choose your IDE:**

   - :doc:`Cursor <integrations>` - AI-powered code editor
   - :doc:`Codex <integrations>` - Terminal-based coding assistant
   - :doc:`Roo Code / Kilo Code <integrations>` - VS Code extensions

3. **Configure and start coding!**

See the :doc:`quickstart` guide for detailed setup instructions.

Available Models
----------------

.. list-table::
   :header-rows: 1
   :widths: 40 30 30

   * - Model
     - Context Length
     - Best For
   * - **GLM-5** :sup:`recommended`
     - 200K tokens
     - Most capable, bilingual
   * - GLM-4.7
     - 200K tokens
     - Long context, bilingual
   * - GLM-4.7-Flash
     - 200K tokens
     - Fast and cost-effective
   * - **MiniMax M2.5** :sup:`new`
     - 1M tokens
     - Ultra-long context, multimodal
   * - MiniMax M2
     - 196K tokens
     - Large codebases
   * - Qwen3 Coder 30B
     - 32K tokens
     - Code generation
   * - Llama 3.3 70B :sup:`limited`
     - 131K tokens
     - General coding tasks
   * - Llama 4 Maverick :sup:`limited`
     - 128K tokens
     - Multimodal support
   * - **BGE-M3** (Embedding)
     - 8K tokens
     - Codebase indexing

See the complete :doc:`models` list for all available models.

Support
-------

Need help? Check out:

* :doc:`integrations` - IDE setup guides
* :doc:`models` - Available models
* GitHub Issues - Report bugs or request features
