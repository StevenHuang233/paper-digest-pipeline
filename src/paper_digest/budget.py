from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Budget:
    max_tokens: int
    max_usd: float
    input_price: float
    output_price: float
    reserved_tokens: int = 0
    reserved_usd: float = 0.0

    @staticmethod
    def estimate_tokens(text: str) -> int:
        # Conservative for mixed Chinese/English and equations.
        return max(1, int(len(text) / 2.8))

    def estimate(self, input_text: str, max_output_tokens: int) -> tuple[int, float]:
        input_tokens = self.estimate_tokens(input_text)
        total = input_tokens + max_output_tokens
        cost = input_tokens * self.input_price / 1_000_000 + max_output_tokens * self.output_price / 1_000_000
        return total, cost

    def reserve(self, input_text: str, max_output_tokens: int) -> tuple[int, float]:
        tokens, cost = self.estimate(input_text, max_output_tokens)
        if self.reserved_tokens + tokens > self.max_tokens:
            raise RuntimeError(f"Token budget exceeded: need {tokens}, remaining {self.max_tokens - self.reserved_tokens}")
        if self.reserved_usd + cost > self.max_usd:
            raise RuntimeError(f"Cost budget exceeded: need ${cost:.4f}, remaining ${self.max_usd - self.reserved_usd:.4f}")
        self.reserved_tokens += tokens
        self.reserved_usd += cost
        return tokens, cost


def from_config(config: dict) -> Budget:
    return Budget(
        max_tokens=int(config["max_total_tokens"]),
        max_usd=float(config["max_estimated_usd"]),
        input_price=float(config["input_usd_per_million"]),
        output_price=float(config["output_usd_per_million"]),
    )

