"""
Bioinformatics Agent — conversational entry point.

Usage:
    python -m agent.main
    python -m agent.main --once "install latest bwa"
"""

import argparse
import json
import sys
from pathlib import Path

import anthropic
import yaml
from dotenv import load_dotenv

load_dotenv()

from agent.tools import OUTER_TOOLS, dispatch_outer_tool

CONFIG_PATH = Path(__file__).parent.parent / "config" / "agent_config.yaml"

SYSTEM_PROMPT = """You are a bioinformatics software agent that helps researchers install, \
validate, and containerize bioinformatics tools and pipelines.

You can:
- Install any bioinformatics tool or multi-tool pipeline into an isolated conda environment
- Automatically find the correct package, version, and install channel from the internet
- Run the installed tools against appropriate test data to verify they work
- Chain pipeline steps so each tool's output feeds the next
- Package everything into an HPC-compatible Docker image

When a user asks you to install something — whether a single tool like "bwa" or a pipeline \
like "STAR + featureCounts" — call the install_pipeline tool. You decide the pipeline name \
if the user doesn't specify one.

When a user asks what genomes or test data are available, call list_available_resources.

When a user asks what has already been installed, call list_installed_pipelines.

Always confirm your plan before starting a long install. Be explicit about what you find \
and what you are doing. If something fails, explain why and what you tried."""


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


class BioinformaticsAgent:
    def __init__(self, config: dict):
        self.config = config
        self.client = anthropic.Anthropic()
        self.history: list[dict] = []

    def chat(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})

        while True:
            response = self.client.messages.create(
                model=self.config["agent"]["model"],
                max_tokens=4096,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=OUTER_TOOLS,
                messages=self.history,
            )

            self.history.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                return self._extract_text(response.content)

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        print(f"\n[agent] → {block.name}({_summarize(block.input)})")
                        result = dispatch_outer_tool(block.name, block.input, self.config)
                        print(f"[agent] ← {block.name} done")
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result),
                            }
                        )

                self.history.append({"role": "user", "content": tool_results})

    @staticmethod
    def _extract_text(content) -> str:
        parts = []
        for block in content:
            if hasattr(block, "text"):
                parts.append(block.text)
        return "\n".join(parts)


def _summarize(inp: dict) -> str:
    short = {k: (str(v)[:60] + "…" if len(str(v)) > 60 else v) for k, v in inp.items()}
    return json.dumps(short)


def run_interactive(config: dict):
    agent = BioinformaticsAgent(config)
    print("Bioinformatics Agent ready. Type 'exit' to quit.\n")
    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            break
        reply = agent.chat(user_input)
        print(f"\nAgent: {reply}\n")


def run_once(config: dict, message: str):
    agent = BioinformaticsAgent(config)
    reply = agent.chat(message)
    print(reply)


def main():
    parser = argparse.ArgumentParser(description="Bioinformatics Agent")
    parser.add_argument("--once", metavar="MSG", help="Run a single message then exit")
    args = parser.parse_args()

    config = load_config()

    if args.once:
        run_once(config, args.once)
    else:
        run_interactive(config)


if __name__ == "__main__":
    main()
