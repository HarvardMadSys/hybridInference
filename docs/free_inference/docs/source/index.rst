FreeInference Documentation
===================================

**Free LLM inference for coding agents and IDEs**

FreeInference provides free access to state-of-the-art language models
for coding agents and AI-powered IDEs, with a particularly smooth setup path for Kilo Code.

Quick Links
-----------

* :doc:`quickstart` - Get started in 5 minutes
* :doc:`integrations` - Configure with Kilo Code, Cursor, Roo Code, and other coding agents
* :doc:`models` - View available models
* :doc:`api_headers` - API headers reference

.. toctree::
   :maxdepth: 2
   :caption: Getting Started:
   :hidden:

   quickstart
   integrations
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
   :widths: 40 30 30

   * - Model
     - Context Length
     - Best For
   * - GLM-5.1
     - 200K tokens
     - Latest GLM-5 generation
   * - **GLM-5 Turbo** :sup:`recommended`
     - 200K tokens
     - Faster GLM-5 variant
   * - GLM-4.7
     - 200K tokens
     - Long context, bilingual
   * - MiniMax M2.5
     - 205K tokens
     - Ultra-long context, multimodal
   * - MiniMax M2.7
     - 205K tokens
     - Large codebases
   * - Qwen3.6 35B :sup:`fastest`
     - 135K tokens
     - Strong reasoning and coding intelligence

See the complete :doc:`models` list for all available models.

Support
-------

Need help? Check out:

* :doc:`integrations` - IDE setup guides
* :doc:`models` - Available models
* GitHub Issues - Report bugs or request features
