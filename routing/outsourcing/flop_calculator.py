"""FLOP calculator abstraction and implementations."""

from abc import ABC, abstractmethod

from .request import OutsourcingRequestInfo


class FLOPCalculatorInterface(ABC):
    """Abstract interface for computing FLOPs required for request processing.

    Hides model architecture details from the outsourcing logic.
    """

    @abstractmethod
    def compute_prefill_flops(self, request: OutsourcingRequestInfo, num_tokens: int) -> float:
        """Calculate FLOPs needed to prefill `num_tokens` for this request.

        Accounts for: hidden_dim, num_layers, num_attention_heads, etc.
        """
        pass

    @abstractmethod
    def get_device_flops_per_second(self) -> float:
        """Peak FLOPs/second of the serving device (e.g., A100 = 312 TFLOPs)."""
        pass

    @abstractmethod
    def get_effective_flops_per_second(self, utilization: float = 0.8) -> float:
        """Effective FLOPs/second accounting for utilization efficiency."""
        pass

    @abstractmethod
    def compute_decode_flops(self, request: OutsourcingRequestInfo, num_tokens: int) -> float:
        """Calculate FLOPs needed to decode `num_tokens` for this request.

        Accounts for KV cache attention and FFN computation.
        """
        pass


class SimpleFLOPCalculator(FLOPCalculatorInterface):
    """Simple FLOP calculator based on transformer architecture."""

    def __init__(
        self,
        hidden_dim: int = 4096,
        num_layers: int = 32,
        num_attention_heads: int = 32,
        device_tflops: float = 312.0,  # A100
    ):
        self.hidden_dim = hidden_dim
        self.num_layers = num_layers
        self.num_attention_heads = num_attention_heads
        self.device_flops = device_tflops * 1e12

    def compute_prefill_flops(self, request: OutsourcingRequestInfo, num_tokens: int) -> float:
        """Prefill: O(n^2 * d) attention + O(n * d^2) FFN.

        where n = sequence length, d = hidden_dim.
        """
        # Attention: 2 * n^2 * d per layer (QK^T + softmax * V)
        attn_flops = 2 * num_tokens * num_tokens * self.hidden_dim * self.num_layers
        # FFN: 4 * n * d^2 per layer (two linear projections)
        ffn_flops = 4 * num_tokens * self.hidden_dim * self.hidden_dim * self.num_layers
        return attn_flops + ffn_flops

    def get_device_flops_per_second(self) -> float:
        """Return the theoretical device FLOPS per second."""
        return self.device_flops

    def get_effective_flops_per_second(self, utilization: float = 0.8) -> float:
        """Return effective FLOPS/second accounting for utilization."""
        return self.device_flops * utilization

    def compute_decode_flops(self, request: OutsourcingRequestInfo, num_tokens: int) -> float:
        """Decode: O(n * d^2) FFN + O(n * k * d) KV attention.

        where n = number of decode tokens, k = KV cache length, d = hidden_dim.
        """
        # KV attention: O(n * k * d) where k is the KV cache length
        kv_cache_len = request.num_prompt_tokens + request.num_processed_tokens
        attn_flops = num_tokens * kv_cache_len * self.hidden_dim * self.num_layers
        # FFN: 4 * n * d^2 per layer (two linear projections)
        ffn_flops = 4 * num_tokens * self.hidden_dim * self.hidden_dim * self.num_layers
        return attn_flops + ffn_flops
