#!/usr/bin/env python3

import asyncio
import json
import time
from dataclasses import dataclass

import aiohttp
import numpy as np


@dataclass
class TestConfig:
    name: str
    base_url: str
    api_key: str
    model: str


@dataclass
class Result:
    latency_ms: float
    tokens: int
    success: bool
    error: str = None


class AdvancedBenchmark:
    """Simplified, elegant benchmark for LLM endpoints."""

    def __init__(self, servers: list[TestConfig]):
        self.servers = servers
        self.prompts = self._load_prompts()

    def _load_prompts(self) -> dict[int, list[str]]:
        """Generate prompts of various lengths."""
        prompts = {}

        # Short prompts (32 tokens ~ 128 chars)
        prompts[32] = [
            "What is machine learning?",
            "Explain quantum computing briefly.",
            "How does blockchain work?",
            "What are neural networks?",
            "Define artificial intelligence.",
        ]

        # Medium prompts (128 tokens ~ 512 chars)
        prompts[128] = [
            "Explain the differences between supervised and unsupervised learning in machine learning. Provide examples of each approach.",
            "Describe how transformers work in natural language processing. What makes them different from RNNs?",
            "What are the main challenges in scaling distributed systems? How can they be addressed?",
        ]

        # Long prompts (512 tokens ~ 2048 chars)
        prompts[512] = [
            """Analyze the impact of artificial intelligence on modern society. Consider the following aspects:
            1. Economic implications including job displacement and creation
            2. Ethical considerations around bias and fairness
            3. Privacy and security concerns
            4. Potential benefits in healthcare, education, and research
            5. Long-term societal changes and adaptations needed
            Provide a balanced perspective with specific examples."""
        ]

        # Very long prompts (2048 tokens ~ 8192 chars)
        prompts[2048] = [
            """You are analyzing a complex distributed system architecture. The system consists of:
            - Multiple microservices handling different business domains
            - A message queue system for asynchronous communication
            - Multiple databases including SQL and NoSQL stores
            - A caching layer using Redis
            - Load balancers and API gateways
            - Container orchestration with Kubernetes

            The system is experiencing the following issues:
            1. Intermittent latency spikes during peak hours
            2. Occasional data inconsistencies between services
            3. Memory leaks in some services requiring frequent restarts
            4. Database connection pool exhaustion
            5. Cache invalidation problems leading to stale data

            For each issue, provide:
            - Root cause analysis
            - Short-term mitigation strategies
            - Long-term architectural improvements
            - Monitoring and alerting recommendations
            - Best practices to prevent recurrence

            Consider trade-offs between consistency, availability, and partition tolerance.
            Discuss how you would prioritize fixes based on business impact."""
        ]

        return prompts

    async def test_endpoint(
        self, session: aiohttp.ClientSession, server: TestConfig, prompt: str, max_tokens: int = 128
    ) -> Result:
        """Test a single endpoint with a prompt."""
        headers = {"Content-Type": "application/json"}
        if server.api_key and server.api_key != "EMPTY":
            headers["Authorization"] = f"Bearer {server.api_key}"

        payload = {
            "model": server.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.7,
        }

        start = time.perf_counter()
        try:
            async with session.post(
                f"{server.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                latency = (time.perf_counter() - start) * 1000
                tokens = data.get("usage", {}).get("completion_tokens", max_tokens)
                return Result(latency, tokens, True)
        except Exception as e:
            return Result(0, 0, False, str(e))

    async def run_test(
        self, server: TestConfig, prompt_length: int, concurrency: int, num_requests: int = 50
    ) -> dict:
        """Run a test with specified parameters."""
        prompts = self.prompts[prompt_length]
        results = []

        async with aiohttp.ClientSession() as session:
            tasks = []
            for i in range(num_requests):
                prompt = prompts[i % len(prompts)]
                task = self.test_endpoint(session, server, prompt)
                tasks.append(task)

                if len(tasks) >= concurrency:
                    batch = await asyncio.gather(*tasks)
                    results.extend(batch)
                    tasks = []

            if tasks:
                batch = await asyncio.gather(*tasks)
                results.extend(batch)

        # Calculate metrics
        successful = [r for r in results if r.success]
        if not successful:
            return {"error": "All requests failed"}

        latencies = [r.latency_ms for r in successful]
        return {
            "server": server.name,
            "prompt_length": prompt_length,
            "concurrency": concurrency,
            "success_rate": len(successful) / len(results),
            "p50": np.percentile(latencies, 50),
            "p95": np.percentile(latencies, 95),
            "p99": np.percentile(latencies, 99),
            "throughput": len(successful) / (sum(latencies) / 1000),
            "avg_tokens_sec": np.mean([r.tokens / (r.latency_ms / 1000) for r in successful]),
        }

    async def run_full_benchmark(self):
        """Run complete benchmark suite."""
        results = []

        for server in self.servers:
            print(f"\nBenchmarking {server.name}")
            print("=" * 50)

            for prompt_length in [32, 128, 512, 2048]:
                for concurrency in [1, 4, 16, 32]:
                    print(f"Testing: {prompt_length} tokens, {concurrency} concurrent")
                    result = await self.run_test(server, prompt_length, concurrency)
                    results.append(result)

                    if "error" not in result:
                        print(f"  ✓ P50: {result['p50']:.0f}ms, P95: {result['p95']:.0f}ms")
                        print(f"  ✓ Throughput: {result['throughput']:.2f} req/s")

        # Save results
        with open("/root/hybridInference/llama_benchmark/results.json", "w") as f:
            json.dump(results, f, indent=2)

        return results


async def main():
    servers = [
        TestConfig(
            name="local_vllm",
            base_url="http://198.51.100.34:9001/v1",
            api_key="EMPTY",
            model="/models/meta-llama_Llama-4-Scout-17B-16E",
        ),
        # Add Llama API config here if needed
        # TestConfig(
        #     name="llama_api",
        #     base_url="https://api.llama.com/compat/v1",
        #     api_key=os.getenv("LLAMA_API_KEY"),
        #     model="Llama-4-Scout-17B-16E-Instruct-FP8"
        # )
    ]

    benchmark = AdvancedBenchmark(servers)
    await benchmark.run_full_benchmark()


if __name__ == "__main__":
    asyncio.run(main())
