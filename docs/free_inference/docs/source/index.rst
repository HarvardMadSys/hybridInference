FreeInference Documentation
===================================

**Free LLM inference for coding agents and IDEs**

FreeInference provides free access to state-of-the-art language models
specifically designed for coding agents like Cursor, Roo Code, and other AI-powered IDEs.

Quick Links
-----------

* :doc:`quickstart` - Get started in 5 minutes
* :doc:`integrations` - Configure with Cursor, Roo Code, and other coding agents
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
   Access GLM, Qwen, MiniMax, and other powerful models

**IDE Integration**
   Easy setup with Cursor, Roo Code, Kilo Code, and more



Getting Started
---------------

1. **Get your API key** - Register at `https://freeinference.org <https://freeinference.org>`_ and create your API key

2. **Choose your IDE:**

   - :doc:`Cursor <integrations>` - AI-powered code editor
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
   * - GLM-5.1
     - 200K tokens
     - Latest GLM-5 generation
   * - GLM-5 Turbo
     - 200K tokens
     - Faster GLM-5 variant
   * - GLM-4.7
     - 200K tokens
     - Long context, bilingual
   * - **MiniMax M2.5** :sup:`new`
     - 1M tokens
     - Ultra-long context, multimodal
   * - MiniMax M2.7
     - 196K tokens
     - Large codebases
   * - Qwen3.6 27B
     - 65K tokens
     - Self-hosted code generation
   * - Qwen3.6 35B
     - 65K tokens
     - Self-hosted code generation

See the complete :doc:`models` list for all available models.

Support
-------

Need help? Check out:

* :doc:`integrations` - IDE setup guides
* :doc:`models` - Available models
* GitHub Issues - Report bugs or request features
