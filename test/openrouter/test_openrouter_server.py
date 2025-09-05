#!/usr/bin/env python3
"""
Test script for the OpenAI-compatible Llama API proxy server.
Tests both streaming and non-streaming functionality.
Can be run as standalone script or with pytest.
"""

import argparse
import json
import time

import requests


class ServerTester:
    """Test the OpenAI-compatible server functionality."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.available_model = None  # Will be set by test_models_endpoint

    def test_health_check(self) -> bool:
        """Test the health check endpoint."""
        print("Testing health check endpoint...")
        try:
            response = self.session.get(f"{self.base_url}/health")
            if response.status_code == 200:
                data = response.json()
                print(f"✓ Health check passed: {data}")
                return True
            else:
                print(f"✗ Health check failed: {response.status_code}")
                return False
        except Exception as e:
            print(f"✗ Health check error: {e}")
            return False

    def test_non_streaming_completion(self) -> bool:
        """Test non-streaming chat completion."""
        print("\nTesting non-streaming chat completion...")

        print(f"Available model: {self.available_model}")
        payload = {
            "model": self.available_model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {
                    "role": "user",
                    "content": "Hello! Please respond with just 'Hello, World! I am a test.'",
                },
            ],
            "max_tokens": 50,
            "temperature": 0.1,
        }

        try:
            response = self.session.post(
                f"{self.base_url}/v1/chat/completions", json=payload, timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                print("✓ Non-streaming request successful")
                print(f"  Response status: {response.status_code}")
                # print(f"  Response: {data}")
                print(f"  Model: {data.get('model', 'N/A')}")
                if "choices" in data and len(data["choices"]) > 0:
                    content = data["choices"][0]["message"]["content"]
                    print(f"  Response: {content[:100]}" + ("..." if len(content) > 100 else ""))
                if "usage" in data:
                    usage = data["usage"]
                    print(f"  Usage: {usage}")
                return True
            else:
                print(f"✗ Non-streaming request failed: {response.status_code}")
                print(f"  Response: {response.text}")
                return False

        except Exception as e:
            print(f"✗ Non-streaming request error: {e}")
            return False

    def test_streaming_completion(self) -> bool:
        """Test streaming chat completion."""
        print("\nTesting streaming chat completion...")

        payload = {
            "model": self.available_model,
            "messages": [
                # {"role": "user", "content": "Count from 1 to 5 slowly, one number per line."}
                {
                    "role": "user",
                    "content": "Hello! Please respond with just 'Hello, World! I am a test.'",
                }
            ],
            "max_tokens": 100,
            "temperature": 0.1,
            "stream": True,
        }

        try:
            response = self.session.post(
                f"{self.base_url}/v1/chat/completions", json=payload, timeout=30, stream=True
            )

            if response.status_code == 200:
                print("✓ Streaming request successful")
                # print("  Streaming chunks:")

                chunk_count = 0
                full_content = ""

                for line in response.iter_lines():
                    if line:
                        line = line.decode("utf-8")
                        if line.startswith("data: "):
                            data_str = line[6:]  # Remove 'data: ' prefix
                            if data_str.strip() == "[DONE]":
                                print("  [DONE]")
                                break

                            try:
                                chunk_data = json.loads(data_str)
                                chunk_count += 1

                                if "choices" in chunk_data and len(chunk_data["choices"]) > 0:
                                    delta = chunk_data["choices"][0].get("delta", {})
                                    if "content" in delta:
                                        content = delta["content"]
                                        full_content += content
                                        # print(f"  Chunk {chunk_count}: '{content}'")

                                # Print usage info if present
                                if "usage" in chunk_data:
                                    print(f"  Usage info: {chunk_data['usage']}")

                            except json.JSONDecodeError:
                                continue

                print(f"  Total chunks received: {chunk_count}")
                print(f"  Full content: '{full_content}'")
                return True
            else:
                print(f"✗ Streaming request failed: {response.status_code}")
                print(f"  Response: {response.text}")
                return False

        except Exception as e:
            print(f"✗ Streaming request error: {e}")
            return False

    def test_models_endpoint(self) -> bool:
        """Test the models endpoint."""
        print("\nTesting models endpoint...")

        try:
            response = self.session.get(f"{self.base_url}/v1/models", timeout=10)

            if response.status_code == 200:
                data = response.json()
                print("✓ Models endpoint successful")
                if "data" in data:
                    models = data["data"]
                    print(f"  Available models: {len(models)}")
                    for model in models[:5]:  # Show first 5 models
                        print(f"    - {model.get('id', 'Unknown')}")
                    if len(models) > 5:
                        print(f"    ... and {len(models) - 5} more")
                    # Store first available model for other tests
                    if models:
                        self.available_model = models[0].get("id")
                return True
            else:
                print(f"✗ Models endpoint failed: {response.status_code}")
                print(f"  Response: {response.text}")
                return False

        except Exception as e:
            print(f"✗ Models endpoint error: {e}")
            return False

    def run_all_tests(self) -> bool:
        """Run all tests and return overall success."""
        print("Running OpenAI-compatible server tests...\n")

        tests = [
            self.test_health_check,
            self.test_models_endpoint,  # Test models first to get available model
            self.test_non_streaming_completion,
            self.test_streaming_completion,
        ]

        results = []
        for test in tests:
            results.append(test())

        print(f"\n{'=' * 50}")
        print(f"Test Results: {sum(results)}/{len(results)} passed")

        if all(results):
            print("✓ All tests passed!")
            return True
        else:
            print("✗ Some tests failed")
            return False


def main():
    """Main function to run tests."""
    parser = argparse.ArgumentParser(description="Test the OpenAI-compatible server")
    parser.add_argument(
        "--url", default="http://localhost:8080", help="Base URL of the server to test"
    )
    parser.add_argument("--wait", type=int, default=2, help="Seconds to wait for server to start")

    args = parser.parse_args()

    if args.wait > 0:
        print(f"Waiting {args.wait} seconds for server to start...")
        time.sleep(args.wait)

    tester = ServerTester(args.url)
    success = tester.run_all_tests()

    exit(0 if success else 1)


# Global tester instance for pytest
_tester = None


def get_tester():
    """Get or create tester instance."""
    global _tester
    if _tester is None:
        _tester = ServerTester("http://localhost:8080")
    return _tester


# Pytest-compatible test functions
def test_health_check():
    """Pytest version of health check test."""
    tester = get_tester()
    assert tester.test_health_check()


def test_models_endpoint():
    """Pytest version of models endpoint test."""
    tester = get_tester()
    assert tester.test_models_endpoint()


def test_non_streaming_completion():
    """Pytest version of non-streaming completion test."""
    tester = get_tester()
    if tester.available_model is None:
        tester.test_models_endpoint()  # Get available model first
    assert tester.test_non_streaming_completion()


def test_streaming_completion():
    """Pytest version of streaming completion test."""
    tester = get_tester()
    if tester.available_model is None:
        tester.test_models_endpoint()  # Get available model first
    assert tester.test_streaming_completion()


if __name__ == "__main__":
    main()
