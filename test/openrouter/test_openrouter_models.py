"""Test script for OpenRouter list_models functionality."""

import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serving.providers.openrouter import OpenRouterProvider


async def test_list_models():
    """Test listing models from OpenRouter provider."""
    
    # Test with local server configuration
    config = {
        "base_url": "http://localhost:8080/v1",
        # No API key needed for local server
    }
    
    provider = OpenRouterProvider(config)
    
    try:
        print("Fetching models from OpenRouter provider...")
        models = await provider.list_models()
        
        print(f"\nFound {len(models)} models:")
        for model in models:
            print(f"  - {model.get('id', 'unknown')}: {model.get('name', 'Unknown')}")
            if 'pricing' in model:
                pricing = model['pricing']
                print(f"    Pricing: ${pricing.get('prompt', '0')}/token (prompt), "
                      f"${pricing.get('completion', '0')}/token (completion)")
            if 'context_length' in model:
                print(f"    Context length: {model['context_length']} tokens")
            print()
            
    except Exception as e:
        print(f"Error fetching models: {e}")
        print("\nMake sure the OpenRouter server is running:")
        print("  python -m serving.servers.openrouter")


if __name__ == "__main__":
    asyncio.run(test_list_models())