"""Agent adapters. An agent takes a prompt and a working directory and
edits files in place; file-level permissions are enforced by the sandbox,
never by the agent itself."""

from __future__ import annotations

from ..config import AgentConfig
from .base import Agent, AgentResult
from .claude_code import ClaudeCodeAgent
from .command import CommandAgent
from .api_agents import AnthropicAPIAgent, OpenAICompatAgent


def create_agent(cfg: AgentConfig) -> Agent:
    if cfg.type == "claude_code":
        return ClaudeCodeAgent(cfg)
    if cfg.type == "anthropic_api":
        return AnthropicAPIAgent(cfg)
    if cfg.type == "openai_compat":
        return OpenAICompatAgent(cfg)
    if cfg.type == "command":
        return CommandAgent(cfg)
    raise ValueError(f"unknown agent type: {cfg.type}")


__all__ = ["Agent", "AgentResult", "create_agent", "ClaudeCodeAgent",
           "CommandAgent", "AnthropicAPIAgent", "OpenAICompatAgent"]
